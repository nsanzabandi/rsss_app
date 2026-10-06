"""
config/email_config.py — SMTP email settings.
Values are read from environment variables (set in .env).
"""
import os


class EmailConfig:
    SMTP_HOST     = os.environ.get("EMAIL_HOST",     "smtp.gmail.com")
    SMTP_PORT     = int(os.environ.get("EMAIL_PORT", 587))
    SMTP_USER     = os.environ.get("EMAIL_USER",     "").strip()
    # Gmail shows the 16-char app password in 4 spaced groups (e.g. "abcd efgh …")
    # but rejects login if the spaces are included — strip all whitespace.
    SMTP_PASSWORD = "".join(os.environ.get("EMAIL_PASSWORD", "").split())
    FROM_ADDRESS  = os.environ.get("EMAIL_FROM",     "RSSS Stunting Reports <danielnsanzabandi@gmail.com>")
    USE_TLS       = True
    TIMEOUT       = 30          # seconds
    # Pause between sends. Was a fixed 2s — ~2 min of pure waiting per run.
    RATE_LIMIT_DELAY = float(os.environ.get("EMAIL_RATE_LIMIT_DELAY", "0.5"))


email_config = EmailConfig()
