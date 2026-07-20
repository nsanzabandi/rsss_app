
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ── Report auto-send schedule (persisted) ─────────────────────────────────────

_SCHEDULE_PATH = BASE_DIR / "config" / "report_schedule.json"


def load_report_schedule() -> dict:
    default = {"enabled": False, "day": 1, "level": "hospital", "dry_run": False}
    try:
        if _SCHEDULE_PATH.exists():
            return {**default, **json.loads(_SCHEDULE_PATH.read_text())}
    except Exception:
        pass
    return default


def save_report_schedule(sched: dict) -> None:
    _SCHEDULE_PATH.write_text(json.dumps(sched, indent=2))


def list_targets(level: str = "hospital") -> list[str]:
    """Distinct hospital (or facility) names in the active backend."""
    from config.backend import get_conn, schema
    s = schema()
    col = s["hospital"] if level == "hospital" else s["facility"]
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(f"SELECT DISTINCT {col} FROM {s['table']} WHERE {col} IS NOT NULL")
        names = [r[0] for r in cur.fetchall() if r[0]]
        cur.close(); conn.close()
        return sorted(names)
    except Exception:
        return []

# ── In-memory job store ────────────────────────────────────────────────────────

_jobs: dict = {}
_lock = threading.Lock()

_MONTHS = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def _norm_name(s: str) -> str:
    """Normalise a facility name for matching: lowercase, drop facility-type
    words and punctuation. So 'Bushenge Sub District' and 'Bushenge District
    Hospital' both match 'bushenge'."""
    import re
    s = (s or "").lower()
    for w in ("district hospital", "sub district", "subdistrict",
              "referral hospital", "hospital", "district"):
        s = s.replace(w, " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def new_job(kind: str, params: dict) -> str:
    jid = str(uuid.uuid4())
    with _lock:
        _jobs[jid] = dict(
            id=jid, kind=kind, params=params,
            status="pending", progress=0, total=0,
            task="", results=[], errors=[],
            created=datetime.now().isoformat(),
            started=None, finished=None,
        )
    return jid


def get_job(jid: str) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        return dict(j) if j else None


def _patch(jid: str, **kw) -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def _push_result(jid: str, r: dict) -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid]["results"].append(r)


def _push_error(jid: str, msg: str) -> None:
    with _lock:
        if jid in _jobs:
            _jobs[jid]["errors"].append(msg)


# ── Hospital reports worker ────────────────────────────────────────────────────

def _run_hospital_reports(
    jid: str,
    hospitals: list[str],
    month: int,
    year: int,
    send_emails: bool,
    triggered_by: str,
    dry_run: bool = False,
) -> None:
    _patch(jid, status="running", started=datetime.now().isoformat(),
           total=len(hospitals), task="Loading data…")
    try:
        from config import app_config
        from core.data_loader import StuntingDataLoader
        from core.stunting_analytics import StuntingAnalytics
        from core.chart_generator import ChartGenerator
        from core.report_generator import HospitalReportGenerator
        from core.email_sender import EmailSender, build_cc_list_from_contact
        from utils.logger import ReportLogger

        log    = ReportLogger(f"job_{jid[:8]}")
        loader = StuntingDataLoader(log)
        groups = loader.load_and_process(year, month)

        if not groups:
            _patch(jid, status="failed", task="No data for this period.",
                   finished=datetime.now().isoformat())
            return

        hosp_set  = set(hospitals)
        all_keys  = list(groups.keys())
        groups    = {k: v for k, v in groups.items() if k in hosp_set}
        if not groups:
            _patch(jid, status="failed",
                   task=f"No match. Available: {all_keys[:5]}",
                   finished=datetime.now().isoformat())
            return

        vacc_counts   = loader.get_vaccinated_counts_by_hospital()
        contacts_path = BASE_DIR / "config" / "hospital_emails.json"
        contacts: dict = {}
        if contacts_path.exists():
            with open(contacts_path) as f:
                raw = json.load(f)
            contacts = {
                h["hospital_name"]: h
                for h in raw.get("hospitals", [])
                if h.get("active", True)
            }
        contacts_norm = {_norm_name(k): v for k, v in contacts.items()}

        reports_dir = BASE_DIR / app_config.REPORTS_DIR
        reports_dir.mkdir(parents=True, exist_ok=True)

        analytics = StuntingAnalytics(log)
        chart_gen  = ChartGenerator(log)
        rep_gen    = HospitalReportGenerator(log)

        # dry_run alone should still exercise the email path (send to TEST_EMAIL),
        # so treat it as "emailing enabled".
        should_email = send_emails or dry_run
        email_sender, connected = None, False
        if should_email:
            email_sender = EmailSender(log)
            connected    = email_sender.connect()
            if not connected:
                _push_error(jid, "Email enabled but SMTP connection failed — "
                                 "check EMAIL_USER/EMAIL_PASSWORD in .env.")

        for idx, (name, df) in enumerate(groups.items()):
            _patch(jid, progress=idx,
                   task=f"Processing {name} ({idx + 1}/{len(groups)})…")
            try:
                if df is None or df.empty:
                    _push_result(jid, dict(hospital=name, status="skipped", reason="No data"))
                    continue

                district = df["district"].iloc[0] if "district" in df.columns else "Unknown"
                stats    = analytics.calculate_statistics(df)
                sched_c  = analytics.get_schedule_specific_counts(df)

                safe = name.replace(" ", "_").replace("/", "_")
                gc   = str(reports_dir / f"{safe}_gender.png")
                sc   = str(reports_dir / f"{safe}_schedule.png")
                chart_gen.create_gender_pie_chart(stats.get("gender_distribution", {}), gc)
                chart_gen.create_schedule_bar_chart(stats.get("schedule_breakdown", {}), sc)
                charts = {"gender_chart": gc, "schedule_chart": sc}

                fd = stats.get("facility_distribution", {})
                if len(fd) > 1:
                    fc = str(reports_dir / f"{safe}_facilities.png")
                    if chart_gen.create_facility_distribution_chart(fd, fc):
                        charts["facility_chart"] = fc

                total_vacc = vacc_counts.get(name, 0)
                pdf   = str(reports_dir / f"{safe}_Report.pdf")
                excel = str(reports_dir / f"{safe}_Cases.xlsx")
                rep_gen.create_pdf_report(name, district, stats, charts,
                                          year, month, pdf,
                                          total_vaccinated=total_vacc)
                rep_gen.create_excel_report(df, excel)

                for cf in charts.values():
                    try:
                        os.remove(cf)
                    except OSError:
                        pass

                result = dict(
                    hospital=name, status="generated",
                    pdf=pdf, excel=excel,
                    total_cases=stats.get("total_stunted_children", 0),
                    email_sent=False,
                )

                if should_email and connected and email_sender:
                    contact = contacts.get(name) or contacts_norm.get(_norm_name(name))
                    subject = f"Hospital Stunting Report {month:02d}/{year} — {name}"
                    try:
                        from auth import make_view_token
                        view_link = (f"{app_config.APP_BASE_URL}/view?"
                                     f"token={make_view_token('hospital', name)}")
                    except Exception:
                        view_link = None
                    body    = email_sender.create_hospital_email_body(
                        name, month, year, stats, sched_c, dashboard_link=view_link)
                    try:
                        email_sender.server.noop()
                    except Exception:
                        connected = email_sender.connect()
                    if dry_run:
                        # Always deliver the test to TEST_EMAIL so report email can
                        # be verified even when a hospital name doesn't match a contact.
                        real = contact["hospital_email"] if contact else "(no contact matched)"
                        result["email_sent"] = email_sender.send_email(
                            to_email=app_config.TEST_EMAIL, cc_emails=[],
                            subject=f"[TEST] {subject}",
                            body=f"[TEST — real recipient: {real}]\n\n{body}",
                            attachments=[pdf, excel])
                        result["dry_run"] = True
                        result["real_recipient"] = real
                    elif contact:
                        result["email_sent"] = email_sender.send_email(
                            to_email=contact["hospital_email"],
                            cc_emails=build_cc_list_from_contact(contact),
                            subject=subject, body=body,
                            attachments=[pdf, excel])
                    else:
                        result["email_error"] = f"No contact matched for '{name}'"

                # Don't keep sent reports on disk — they've been emailed.
                if should_email:
                    for _f in (pdf, excel):
                        try:
                            os.remove(_f)
                        except OSError:
                            pass

                _push_result(jid, result)

            except Exception as exc:
                _push_error(jid, f"{name}: {exc}\n{traceback.format_exc()}")
                _push_result(jid, dict(hospital=name, status="failed", error=str(exc)))

        if email_sender and connected:
            try:
                email_sender.disconnect()
            except Exception:
                pass

        _patch(jid, status="completed", progress=len(hospitals),
               task="Done.", finished=datetime.now().isoformat())

    except Exception as exc:
        _patch(jid, status="failed", task=str(exc),
               finished=datetime.now().isoformat())
        _push_error(jid, traceback.format_exc())


# ── Facility reports worker ────────────────────────────────────────────────────

def _run_facility_reports(
    jid: str,
    facilities: list[str],
    month: int,
    year: int,
    send_emails: bool,
    triggered_by: str,
    dry_run: bool = False,
) -> None:
    _patch(jid, status="running", started=datetime.now().isoformat(),
           total=len(facilities), task="Loading data…")
    try:
        from config import app_config
        from core.data_loader import StuntingDataLoader
        from core.stunting_analytics import StuntingAnalytics
        from core.chart_generator import ChartGenerator
        from core.report_generator import HospitalReportGenerator
        from core.email_sender import EmailSender
        from utils.logger import ReportLogger

        log    = ReportLogger(f"fac_{jid[:8]}")
        loader = StuntingDataLoader(log)
        loader.load_and_process(year, month)

        if loader.stunted_df is None or loader.stunted_df.empty:
            _patch(jid, status="failed", task="No data for this period.",
                   finished=datetime.now().isoformat())
            return

        groups   = loader.group_by_facility()
        fac_set  = set(facilities)
        all_keys = list(groups.keys())
        groups   = {k: v for k, v in groups.items() if k in fac_set}
        if not groups:
            _patch(jid, status="failed",
                   task=f"No match. Available: {all_keys[:5]}",
                   finished=datetime.now().isoformat())
            return

        vacc_counts   = loader.get_vaccinated_counts_by_facility()
        contacts_path = BASE_DIR / "config" / "health_facility_emails.json"
        contacts: dict = {}
        if contacts_path.exists():
            with open(contacts_path) as f:
                raw = json.load(f)
            contacts = {
                h["facility_name"]: h
                for h in raw.get("facilities", [])
                if h.get("active", True)
            }
        contacts_norm = {_norm_name(k): v for k, v in contacts.items()}

        reports_dir = BASE_DIR / app_config.REPORTS_DIR
        reports_dir.mkdir(parents=True, exist_ok=True)

        analytics = StuntingAnalytics(log)
        chart_gen  = ChartGenerator(log)
        rep_gen    = HospitalReportGenerator(log)

        should_email = send_emails or dry_run
        email_sender, connected = None, False
        if should_email:
            email_sender = EmailSender(log)
            connected    = email_sender.connect()
            if not connected:
                _push_error(jid, "Email enabled but SMTP connection failed — "
                                 "check EMAIL_USER/EMAIL_PASSWORD in .env.")

        for idx, (name, df) in enumerate(groups.items()):
            _patch(jid, progress=idx,
                   task=f"Processing {name} ({idx + 1}/{len(groups)})…")
            try:
                if df is None or df.empty:
                    _push_result(jid, dict(facility=name, status="skipped", reason="No data"))
                    continue

                district = df["district"].iloc[0] if "district" in df.columns else "Unknown"
                hospital = df["district_hospital"].iloc[0] if "district_hospital" in df.columns else "Unknown"
                stats    = analytics.calculate_statistics(df)
                sched_c  = analytics.get_schedule_specific_counts(df)

                safe = name.replace(" ", "_").replace("/", "_")
                gc   = str(reports_dir / f"fac_{safe}_gender.png")
                sc   = str(reports_dir / f"fac_{safe}_schedule.png")
                chart_gen.create_gender_pie_chart(stats.get("gender_distribution", {}), gc)
                chart_gen.create_schedule_bar_chart(stats.get("schedule_breakdown", {}), sc)
                charts = {"gender_chart": gc, "schedule_chart": sc}

                total_vacc = vacc_counts.get(name, 0)
                pdf   = str(reports_dir / f"Fac_{safe}_Report.pdf")
                excel = str(reports_dir / f"Fac_{safe}_Cases.xlsx")
                rep_gen.create_pdf_report(name, district, stats, charts,
                                          year, month, pdf,
                                          total_vaccinated=total_vacc,
                                          report_level="facility")
                rep_gen.create_excel_report(df, excel)

                for cf in charts.values():
                    try:
                        os.remove(cf)
                    except OSError:
                        pass

                result = dict(
                    facility=name, hospital=hospital, status="generated",
                    pdf=pdf, excel=excel,
                    total_cases=stats.get("total_stunted_children", 0),
                    email_sent=False,
                )

                if should_email and connected and email_sender:
                    contact  = contacts.get(name) or contacts_norm.get(_norm_name(name))
                    real_to  = contact["facility_email"] if contact else None
                    cname    = (contact.get("contact_name") or f"{name} Team") if contact else f"{name} Team"
                    subject  = f"Facility Stunting Report {month:02d}/{year} — {name}"
                    body = (
                        f"Dear {cname},\n\n"
                        f"Stunting report for {name} — {_MONTHS[month]} {year}.\n\n"
                        f"Total stunted children: {stats.get('total_stunted_children', 0):,}\n"
                        f"District: {district}  |  Hospital: {hospital}\n\n"
                        f"Please ensure follow-up per national protocols.\n\n"
                        f"Best regards,\nRBC Stunting Surveillance\nTriggered by: {triggered_by}"
                    )
                    try:
                        email_sender.server.noop()
                    except Exception:
                        connected = email_sender.connect()
                    if dry_run:
                        result["email_sent"] = email_sender.send_email(
                            to_email=app_config.TEST_EMAIL, cc_emails=[],
                            subject=f"[TEST] {subject}",
                            body=f"[TEST — real: {real_to or 'not configured'}]\n\n{body}",
                            attachments=[pdf, excel])
                        result["dry_run"] = True
                        result["real_recipient"] = real_to or "(not configured)"
                    elif real_to:
                        result["email_sent"] = email_sender.send_email(
                            to_email=real_to, cc_emails=[],
                            subject=subject, body=body,
                            attachments=[pdf, excel])
                    else:
                        result["email_error"] = "No contact in health_facility_emails.json"

                # Don't keep sent reports on disk — they've been emailed.
                if should_email:
                    for _f in (pdf, excel):
                        try:
                            os.remove(_f)
                        except OSError:
                            pass

                _push_result(jid, result)

            except Exception as exc:
                _push_error(jid, f"{name}: {exc}\n{traceback.format_exc()}")
                _push_result(jid, dict(facility=name, status="failed", error=str(exc)))

        if email_sender and connected:
            try:
                email_sender.disconnect()
            except Exception:
                pass

        _patch(jid, status="completed", progress=len(facilities),
               task="Done.", finished=datetime.now().isoformat())

    except Exception as exc:
        _patch(jid, status="failed", task=str(exc),
               finished=datetime.now().isoformat())
        _push_error(jid, traceback.format_exc())


# ── Public launchers ───────────────────────────────────────────────────────────

def start_hospital_job(
    hospitals: list[str],
    month: int,
    year: int,
    send_emails: bool,
    triggered_by: str,
    dry_run: bool = False,
) -> str:
    jid = new_job("hospital", dict(
        hospitals=hospitals, month=month, year=year,
        send_emails=send_emails, dry_run=dry_run))
    threading.Thread(
        target=_run_hospital_reports,
        args=(jid, hospitals, month, year, send_emails, triggered_by),
        kwargs={"dry_run": dry_run},
        daemon=True,
    ).start()
    return jid


def start_facility_job(
    facilities: list[str],
    month: int,
    year: int,
    send_emails: bool,
    triggered_by: str,
    dry_run: bool = False,
) -> str:
    jid = new_job("facility", dict(
        facilities=facilities, month=month, year=year,
        send_emails=send_emails, dry_run=dry_run))
    threading.Thread(
        target=_run_facility_reports,
        args=(jid, facilities, month, year, send_emails, triggered_by),
        kwargs={"dry_run": dry_run},
        daemon=True,
    ).start()
    return jid
