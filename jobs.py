
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


# ── National overview (senior/ministry officials) ──────────────────────────────

def _send_national_overview(
    jid: str,
    loader,
    month: int,
    year: int,
    dry_run: bool,
    triggered_by: str,
    log,
    reports_dir: Path,
    at_risk_df=None,
    hospitals_notified: int = 0,
) -> None:
    """
    Build a national-level overview PDF (all districts/hospitals combined) and
    email it to config/national_report_recipients.json contacts. Reuses the
    already-loaded `loader` from the calling hospital/facility job — no extra
    DB query.

    at_risk_df: pass the already-computed national at-risk dataframe (from
    _run_hospital_reports, if it also ran) to avoid recomputing it — this is
    the same growth-velocity classification, not a separate one. Pass None to
    compute it fresh (e.g. when called standalone from a facility-only job).
    hospitals_notified: how many hospitals were also individually emailed in
    this same run — mentioned in the overview email body so recipients know
    the underlying hospitals already have their own catchment-area reports.

    Pushes exactly one aggregate result row into the job via _push_result, so
    it shows up in the existing results table without any UI changes.
    """
    import pandas as pd
    from config import app_config
    from core.national_summary_analytics import NationalSummaryAnalytics
    from core.chart_generator import ChartGenerator
    from core.report_generator import HospitalReportGenerator
    from core.email_sender import EmailSender
    from core.risk_classifier import build_risk_df_for_period, get_risk_summary

    ROW_NAME = "National Overview (Senior Officials)"

    if loader.stunted_df is None or loader.stunted_df.empty:
        _push_result(jid, dict(hospital=ROW_NAME, status="skipped",
                               reason="No national data for this period"))
        return

    contacts_path = BASE_DIR / "config" / "national_report_recipients.json"
    contacts: list[dict] = []
    if contacts_path.exists():
        with open(contacts_path) as f:
            raw = json.load(f)
        contacts = [c for c in raw.get("recipients", []) if c.get("active", True)]
    if not contacts:
        _push_result(jid, dict(hospital=ROW_NAME, status="skipped",
                               reason="No active contacts in config/national_report_recipients.json"))
        return

    try:
        nat       = NationalSummaryAnalytics(log)
        chart_gen = ChartGenerator(log)
        rep_gen   = HospitalReportGenerator(log)

        df = loader.stunted_df
        total = len(df)

        gender_flat   = {g: v["count"] for g, v in nat.calculate_gender_distribution(df).items()}
        province_flat = {p: v["count"] for p, v in nat.calculate_province_distribution(df).items()}
        age_dist      = nat.calculate_age_distribution(df)
        avg_age       = nat.calculate_average_age(df)

        hosp_groups = ({name: sub for name, sub in df.groupby("district_hospital")
                       if pd.notna(name) and str(name).strip()}
                      if "district_hospital" in df.columns else {})
        hospital_summary = nat.get_hospital_summary(hosp_groups)
        district_summary = nat.get_district_summary(loader.all_df, loader.stunted_df)
        total_vaccinated = loader.get_total_vaccinated_unique()

        stats = dict(total_stunted_children=total, avg_age_months=avg_age,
                    age_distribution=age_dist)

        # Same underlying numbers as the dashboard's own "Stunting Rate Over
        # Time" chart (data.monthly_rate over data.get_monthly_df()), so the
        # PDF and the live dashboard always tell the same story.
        trend_flat = {}
        try:
            import data as _data
            monthly = _data.get_monthly_df_stale_ok()
            if monthly is not None and not monthly.empty:
                m = _data.monthly_rate(monthly)
                trend_flat = {ts.strftime("%b %Y"): y for ts, y in zip(m["month"], m["y"])}
        except Exception as exc:
            log.warning(f"Trend chart data unavailable: {exc}")

        # At-risk (growth-velocity) — reuse if the caller already computed it
        # for the hospital reports in this same run, else compute fresh.
        if at_risk_df is None:
            try:
                at_risk_df = build_risk_df_for_period(loader.stunted_df)
            except Exception as exc:
                log.warning(f"At-risk computation unavailable: {exc}")
                at_risk_df = pd.DataFrame()
        risk_summary = get_risk_summary(at_risk_df) if at_risk_df is not None else None

        gc = str(reports_dir / "overview_gender.png")
        pc = str(reports_dir / "overview_province.png")
        hc = str(reports_dir / "overview_top_hospitals.png")
        tc = str(reports_dir / "overview_trend.png")
        charts = {}
        if trend_flat and chart_gen.create_stunting_trend_chart(trend_flat, tc):
            charts["trend_chart"] = tc
        if chart_gen.create_gender_pie_chart(gender_flat, gc):
            charts["gender_chart"] = gc
        if chart_gen.create_province_bar_chart(province_flat, pc):
            charts["province_chart"] = pc
        top_hosp_flat = {r["hospital"]: r["total"] for r in hospital_summary[:15]}
        if chart_gen.create_facility_distribution_chart(
                top_hosp_flat, hc, title="Top Hospitals by Stunted Children"):
            charts["top_hospitals_chart"] = hc

        pdf_path = str(reports_dir / f"National_Overview_{year}_{month:02d}.pdf")
        rep_gen.create_national_overview_pdf(
            stats, charts, year, month, pdf_path,
            total_vaccinated=total_vaccinated, hospital_summary=hospital_summary,
            risk_summary=risk_summary, district_summary=district_summary)

        for cf in charts.values():
            try: os.remove(cf)
            except OSError: pass

        # Dashboard link is disabled for now — same flag as the hospital emails.
        view_link = None
        if app_config.INCLUDE_DASHBOARD_LINK_IN_EMAILS:
            try:
                from auth import make_view_token
                view_link = (f"{app_config.APP_BASE_URL}/view?"
                             f"token={make_view_token('ministry', 'national')}")
            except Exception:
                view_link = None

        body = EmailSender(log).create_overview_email_body(
            month, year, stats, n_hospitals=len(hospital_summary),
            total_vaccinated=total_vaccinated, dashboard_link=view_link,
            hospitals_notified=hospitals_notified)
        subject = f"National Stunting Overview {month:02d}/{year}"

        sender = EmailSender(log)
        connected = sender.connect()
        sent = 0
        if connected:
            if dry_run:
                recipients = ", ".join(c["email"] for c in contacts)
                sent = int(sender.send_email(
                    to_email=app_config.TEST_EMAIL, cc_emails=[],
                    subject=f"[TEST] {subject}",
                    body=f"[TEST — real recipients: {recipients}]\n\n{body}",
                    attachments=[pdf_path]))
            else:
                for c in contacts:
                    ok = sender.send_email(
                        to_email=c["email"], cc_emails=[],
                        subject=subject, body=body, attachments=[pdf_path])
                    sent += int(ok)
            sender.disconnect()

        try: os.remove(pdf_path)
        except OSError: pass

        _push_result(jid, dict(
            hospital=ROW_NAME, status="generated", total_cases=total,
            email_sent=sent > 0,
            reason=(f"[TEST] sent to {app_config.TEST_EMAIL}" if dry_run
                    else f"{sent}/{len(contacts)} senior recipient(s) sent"),
        ))

    except Exception as exc:
        _push_error(jid, f"National overview: {exc}\n{traceback.format_exc()}")
        _push_result(jid, dict(hospital=ROW_NAME, status="failed", error=str(exc)))


# ── Hospital reports worker ────────────────────────────────────────────────────

def _run_hospital_reports(
    jid: str,
    hospitals: list[str],
    month: int,
    year: int,
    send_emails: bool,
    triggered_by: str,
    dry_run: bool = False,
    send_overview: bool = False,
) -> None:
    _patch(jid, status="running", started=datetime.now().isoformat(),
           total=len(hospitals), task="Loading data…")
    try:
        import pandas as pd
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

        # At-risk (growth-velocity) children, computed once nationally and
        # filtered per hospital below — attached alongside each hospital's
        # usual report so facilities see who needs follow-up attention now,
        # not just who's currently stunted. Only computed when actually
        # emailing (it's an extra DB query + classification pass).
        at_risk_national = None
        if should_email:
            try:
                from core.risk_classifier import build_risk_df_for_period
                at_risk_national = build_risk_df_for_period(loader.stunted_df)
            except Exception as exc:
                log.warning(f"At-risk computation unavailable: {exc}")
                at_risk_national = pd.DataFrame()

        hospitals_notified = 0

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

                # At-risk (growth-velocity) children for THIS hospital — an
                # additional attachment alongside the usual report/case list,
                # not a replacement. Same national classification, just
                # filtered. HIGH risk only (weight loss between visits, severe
                # wasting, or MUAC<11.5cm) — MEDIUM alone would run into the
                # thousands per hospital and stop being an actionable list.
                attachments  = [pdf, excel]
                at_risk_xlsx = None
                at_risk_n    = 0
                if at_risk_national is not None and not at_risk_national.empty \
                        and "district_hospital" in at_risk_national.columns:
                    hosp_at_risk = at_risk_national[
                        (at_risk_national["district_hospital"] == name) &
                        (at_risk_national["risk_level"] == "HIGH")
                    ]
                    at_risk_n = len(hosp_at_risk)
                    if at_risk_n:
                        at_risk_xlsx = str(reports_dir / f"{safe}_At_Risk.xlsx")
                        if not rep_gen.create_at_risk_excel(hosp_at_risk, at_risk_xlsx):
                            at_risk_xlsx = None
                if at_risk_xlsx:
                    attachments.append(at_risk_xlsx)

                result = dict(
                    hospital=name, status="generated",
                    pdf=pdf, excel=excel,
                    total_cases=stats.get("total_stunted_children", 0),
                    email_sent=False,
                )

                if should_email and connected and email_sender:
                    contact = contacts.get(name) or contacts_norm.get(_norm_name(name))
                    subject = f"Hospital Stunting Report {month:02d}/{year} — {name}"
                    # Dashboard link is disabled for now (config.app_config.
                    # INCLUDE_DASHBOARD_LINK_IN_EMAILS) — flip that flag back to
                    # True to re-enable, no other change needed here.
                    view_link = None
                    if app_config.INCLUDE_DASHBOARD_LINK_IN_EMAILS:
                        try:
                            from auth import make_view_token
                            view_link = (f"{app_config.APP_BASE_URL}/view?"
                                         f"token={make_view_token('hospital', name)}")
                        except Exception:
                            view_link = None
                    body    = email_sender.create_hospital_email_body(
                        name, month, year, stats, sched_c, dashboard_link=view_link,
                        at_risk_count=at_risk_n)
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
                            attachments=attachments)
                        result["dry_run"] = True
                        result["real_recipient"] = real
                    elif contact:
                        result["email_sent"] = email_sender.send_email(
                            to_email=contact["hospital_email"],
                            cc_emails=build_cc_list_from_contact(contact),
                            subject=subject, body=body,
                            attachments=attachments)
                        if result["email_sent"]:
                            hospitals_notified += 1
                    else:
                        result["email_error"] = f"No contact matched for '{name}'"

                # Don't keep sent reports on disk — they've been emailed.
                if should_email:
                    for _f in attachments:
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

        if send_overview and should_email:
            _patch(jid, task="Sending national overview to senior officials…")
            _send_national_overview(jid, loader, month, year, dry_run,
                                    triggered_by, log, reports_dir,
                                    at_risk_df=at_risk_national,
                                    hospitals_notified=hospitals_notified)

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
    send_overview: bool = False,
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

        if send_overview and should_email:
            _patch(jid, task="Sending national overview to senior officials…")
            _send_national_overview(jid, loader, month, year, dry_run,
                                    triggered_by, log, reports_dir)

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
    send_overview: bool = False,
) -> str:
    jid = new_job("hospital", dict(
        hospitals=hospitals, month=month, year=year,
        send_emails=send_emails, dry_run=dry_run, send_overview=send_overview))
    threading.Thread(
        target=_run_hospital_reports,
        args=(jid, hospitals, month, year, send_emails, triggered_by),
        kwargs={"dry_run": dry_run, "send_overview": send_overview},
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
    send_overview: bool = False,
) -> str:
    jid = new_job("facility", dict(
        facilities=facilities, month=month, year=year,
        send_emails=send_emails, dry_run=dry_run, send_overview=send_overview))
    threading.Thread(
        target=_run_facility_reports,
        args=(jid, facilities, month, year, send_emails, triggered_by),
        kwargs={"dry_run": dry_run, "send_overview": send_overview},
        daemon=True,
    ).start()
    return jid
