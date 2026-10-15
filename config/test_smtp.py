"""
Quick SMTP check against the RBC Zimbra server (587 + STARTTLS + passcode login).

Reads EMAIL_HOST / EMAIL_PORT / EMAIL_USER / EMAIL_PASSWORD from the project's
.env (same file the app uses), so just run:
    python3 config/test_smtp.py

Variables set on the command line win over .env, e.g. TEST_TO=someone@rbc.gov.rw.
Add SMTP_DEBUG=1 to see the full SMTP conversation.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid

# --- CONFIGURATION ---
SMTP_HOST = os.environ.get("EMAIL_HOST", "197.243.27.181")
SMTP_PORT = int(os.environ.get("EMAIL_PORT", 587))

SENDER_EMAIL    = os.environ.get("EMAIL_USER", "notifications@rbc.gov.rw")
EMAIL_PASSWORD  = "".join(os.environ.get("EMAIL_PASSWORD", "").split())
RECIPIENT_EMAIL = os.environ.get("TEST_TO", "danielnsanzabandi@gmail.com")


def test_smtp():
    if not EMAIL_PASSWORD:
        print("❌ EMAIL_PASSWORD is empty — put the Zimbra passcode in the project's .env")
        return

    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} (STARTTLS, login as {SENDER_EMAIL})...")
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        # SMTP_DEBUG=1 prints the full conversation — note it includes the
        # AUTH line, which is the passcode in base64. Don't paste it anywhere.
        server.set_debuglevel(1 if os.environ.get("SMTP_DEBUG") else 0)
        server.ehlo()
        server.starttls()
        server.ehlo()
        print("Connection + STARTTLS successful!")

        server.login(SENDER_EMAIL, EMAIL_PASSWORD)
        print("Authentication successful!")

        msg = MIMEMultipart()
        msg["From"] = f"RSSS Notifications <{SENDER_EMAIL}>"
        msg["To"] = RECIPIENT_EMAIL
        msg["Subject"] = "RBC SMTP Test Script"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=SENDER_EMAIL.split("@")[-1])
        msg.attach(MIMEText("This is a test notification from the stunting report app.", "plain"))

        refused = server.sendmail(SENDER_EMAIL, [RECIPIENT_EMAIL], msg.as_string())
        server.quit()
        if refused:
            print("⚠️ Server refused some recipients:", refused)
        else:
            print(f"✅ SUCCESS: Test email sent to {RECIPIENT_EMAIL}")

    except Exception as e:
        print(f"❌ ERROR: {type(e).__name__} — {e}")


if __name__ == "__main__":
    test_smtp()
