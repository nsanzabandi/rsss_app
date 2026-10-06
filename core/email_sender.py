"""
core/email_sender.py — SMTP email sender for RSSS reports.

Features:
  • TLS connection with reconnect on stale connection
  • Rate limiting between sends
  • CC support
  • Attachment support (PDF + Excel)
  • Dry-run mode (redirect to TEST_EMAIL)
"""
from __future__ import annotations

import os
import smtplib
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.logger import ReportLogger

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def build_cc_list_from_contact(contact: dict) -> list[str]:
    """
    Extract CC email addresses from a hospital/facility contact dict.
    Looks for keys: cc_emails (list), cc_email (str), district_email.
    """
    cc: list[str] = []
    if "cc_emails" in contact and isinstance(contact["cc_emails"], list):
        cc.extend(e for e in contact["cc_emails"] if e)
    if "cc_email" in contact and contact["cc_email"]:
        cc.append(contact["cc_email"])
    if "district_email" in contact and contact["district_email"]:
        cc.append(contact["district_email"])
    return list(dict.fromkeys(cc))   # deduplicate, preserve order


class EmailSender:
    def __init__(self, log: "ReportLogger | None" = None):
        # log is optional: callers like the /test-email route and the risk page
        # construct EmailSender() with no arguments. Fall back to a default logger
        # so those paths don't crash with a missing-argument TypeError.
        if log is None:
            from utils.logger import ReportLogger
            log = ReportLogger("email")
        self.log   = log
        self.server: smtplib.SMTP | None = None
        self._sent_count = 0

    def connect(self) -> bool:
        from config.email_config import email_config as cfg
        if not cfg.SMTP_USER or not cfg.SMTP_PASSWORD:
            self.log.error(
                "SMTP credentials missing — EMAIL_USER / EMAIL_PASSWORD are empty. "
                "Create a .env file (copy .env.example) with a valid Gmail address "
                "and 16-character app password, then restart the app."
            )
            self.server = None
            return False
        try:
            self.server = smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT,
                                       timeout=cfg.TIMEOUT)
            self.server.ehlo()
            if cfg.USE_TLS:
                self.server.starttls()
                self.server.ehlo()
            self.server.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            self.log.info(f"SMTP connected to {cfg.SMTP_HOST}:{cfg.SMTP_PORT}")
            return True
        except Exception as exc:
            self.log.error(f"SMTP connect failed: {exc}")
            self.server = None
            return False

    def disconnect(self) -> None:
        if self.server:
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None

    def send_email(
        self,
        to_email: str,
        cc_emails: list[str],
        subject: str,
        body: str,
        attachments: list[str] | None = None,
    ) -> bool:
        from config.email_config import email_config as cfg

        if not self.server:
            self.log.warning("Not connected — reconnecting…")
            if not self.connect():
                return False

        msg = MIMEMultipart()
        msg["From"]    = cfg.FROM_ADDRESS
        msg["To"]      = to_email
        if cc_emails:
            msg["Cc"]  = ", ".join(cc_emails)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        for path in (attachments or []):
            if not path or not os.path.exists(path):
                self.log.warning(f"Attachment not found: {path}")
                continue
            try:
                with open(path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition",
                                "attachment", filename=os.path.basename(path))
                msg.attach(part)
            except Exception as exc:
                self.log.warning(f"Could not attach {path}: {exc}")

        all_recipients = [to_email] + (cc_emails or [])
        try:
            self.server.sendmail(cfg.FROM_ADDRESS, all_recipients, msg.as_string())
            self._sent_count += 1
            self.log.info(f"Email sent → {to_email}  ({self._sent_count} total)")
            time.sleep(cfg.RATE_LIMIT_DELAY)
            return True
        except smtplib.SMTPServerDisconnected:
            self.log.warning("SMTP disconnected — reconnecting…")
            if self.connect():
                return self.send_email(to_email, cc_emails, subject, body, attachments)
            return False
        except Exception as exc:
            self.log.error(f"Send error: {exc}")
            return False

    def create_hospital_email_body(
        self,
        name: str,
        month: int,
        year: int,
        stats: dict,
        schedule_counts: dict | None = None,
        dashboard_link: str | None = None,
        at_risk_count: int = 0,
    ) -> str:
        from datetime import datetime
        from config.app_config import (SUPPORT_PHONE, SUPPORT_CONTACT,
                                        SUPPORT_EMAIL, ORG_NAME, ORG_DIVISION)
        total     = stats.get("total_stunted_children", 0)
        n_fac     = len(stats.get("facility_distribution", {}) or {})
        generated = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = [
            "Dear Hospital Administrator and district officers,",
            "",
            f"Kindly find attached the stunting surveillance report for {name} "
            f"for the period {month:02d}/{year}.",
            "",
            "Report Summary:",
            f"- Total stunted children: {total:,}",
            f"- Health facilities covered: {n_fac}",
            f"- Report generated: {generated}",
            "",
            "This report has been generated by the Rwanda Stunting Surveillance System.",
        ]
        if at_risk_count:
            lines += [
                "",
                f" NOTE: we've also attached a list of {at_risk_count:,} HIGH-risk child(ren) "
                f"in your catchment area (weight loss between visits, severe wasting, or "
                f"MUAC below 11.5cm). We'd appreciate your team giving these cases some "
                f"extra, priority attention.",
            ]
        if dashboard_link:
            lines += [
                "",
                "View your live dashboard (your catchment only, no login needed):",
                dashboard_link,
            ]
        lines += [
            "",
            "For questions or support, please contact:",
            SUPPORT_CONTACT,
            SUPPORT_EMAIL,
            SUPPORT_PHONE,
            "",
            "---",
            ORG_NAME,
            ORG_DIVISION,
        ]
        return "\n".join(lines)

    def create_overview_email_body(
        self,
        month: int,
        year: int,
        stats: dict,
        n_hospitals: int = 0,
        total_vaccinated: int = 0,
        dashboard_link: str | None = None,
        hospitals_notified: int = 0,
        total_assessed: int = 0,
    ) -> str:
        """National overview email body — for senior/ministry officials
        (config/national_report_recipients.json), distinct from the
        per-hospital email."""
        from datetime import datetime
        from config.app_config import (SUPPORT_PHONE, SUPPORT_CONTACT,
                                        SUPPORT_EMAIL, ORG_NAME, ORG_DIVISION)
        total     = stats.get("total_stunted_children", 0)
        denom     = total_assessed or total_vaccinated
        pct       = round(total / denom * 100, 1) if denom else 0
        generated = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = [
            "Dear All,",
            "",
            f"Kindly find attached the national stunting surveillance overview "
            f"for {MONTH_NAMES.get(month, month)} {year}.",
            "",
            "National Summary:",
            f"- Total stunted children (national): {total:,}",
            f"- Children assessed (denominator): {denom:,} ({pct}% stunted)",
            f"- Total vaccinated this period: {total_vaccinated:,}",
            f"- Hospitals reporting this period: {n_hospitals}",
            f"- Report generated: {generated}",
            "",
            "This report has been generated by the Rwanda Stunting Surveillance System.",
        ]
        if hospitals_notified:
            lines += [
                "",
                f"NOTE: Individual reports covering their own catchment areas were also sent "
                f"directly to {hospitals_notified} hospital(s) this period, including a an additional "
                f"list of children flagged for growth-velocity follow-up where applicable.",
            ]
        if dashboard_link:
            lines += [
                "",
                "View the live national dashboard:",
                dashboard_link,
            ]
        lines += [
            "",
            "For questions or support, please contact:",
            SUPPORT_CONTACT,
            SUPPORT_EMAIL,
            SUPPORT_PHONE,
            "",
            "---",
            ORG_NAME,
            ORG_DIVISION,
        ]
        return "\n".join(lines)
