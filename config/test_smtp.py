import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- CONFIGURATION ---
# --- CONFIGURATION ---
SMTP_HOST = "197.243.27.181"  # Use the direct IP provided by Laurette
SMTP_PORT = 25                 # Port 25 worked previously for connection
USE_TLS = False                # Port 25 unencrypted/plain connection
USE_SSL = False                

# Sender and recipient
SENDER_EMAIL = "notifications@rbc.gov.rw"
RECIPIENT_EMAIL = "danielnsanzabandi@gmail.com"

# Keep authentication disabled as she suggested
TRY_AUTHENTICATION = False 
EMAIL_PASSWORD = ""

def test_smtp():
    print(f"Connecting to {SMTP_HOST}:{SMTP_PORT} (TLS={USE_TLS}, SSL={USE_SSL})...")
    try:
        if USE_SSL:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
            if USE_TLS:
                server.starttls()
                
        print("Connection successful!")

        if TRY_AUTHENTICATION:
            print(f"Attempting login as {SENDER_EMAIL}...")
            server.login(SENDER_EMAIL, EMAIL_PASSWORD)
            print("Authentication successful!")
        else:
            print("Skipping authentication (testing IP Relay mode)...")

        # Create a test message
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = RECIPIENT_EMAIL
        msg["Subject"] = "RBC SMTP Test Script"
        msg.attach(MIMEText("This is a test notification from the stunting report app.", "plain"))

        server.sendmail(SENDER_EMAIL, [RECIPIENT_EMAIL], msg.as_string())
        server.quit()
        print("✅ SUCCESS: Test email sent successfully!")

    except Exception as e:
        print(f"❌ ERROR: {type(e).__name__} — {e}")

if __name__ == "__main__":
    test_smtp()