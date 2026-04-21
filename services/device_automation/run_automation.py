"""
run_automation.py — Entry point for GitHub Actions Android emulator automation.

Reads the job from Firestore, decrypts credentials, runs Appium automation
against the local Android emulator, and writes the result back to Firestore.
Sends a Telegram notification when done.
"""
import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_automation")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _firestore_client():
    from google.cloud import firestore
    from google.oauth2 import service_account

    project_id = os.environ["GCP_PROJECT_ID"]
    database = os.environ.get("FIRESTORE_DATABASE", "(default)")
    cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
    if cred_json:
        info = json.loads(cred_json)
        creds = service_account.Credentials.from_service_account_info(info)
        return firestore.Client(project=project_id, credentials=creds, database=database)
    return firestore.Client(project=project_id, database=database)


def _decrypt(ciphertext_b64: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = base64.b64decode(os.environ["ENCRYPTION_KEY"])
    data = base64.b64decode(ciphertext_b64)
    nonce, ct = data[:12], data[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode()


def _get_totp(secret: str) -> str:
    import pyotp
    return pyotp.TOTP(secret.strip().upper()).now()


def _send_telegram(user_id: str, text: str) -> None:
    import requests as req
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": user_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Telegram notify failed: %s", exc)


def _log_event(db, job_id: str, action: str, details: str, level: str = "INFO") -> None:
    import uuid
    db.collection("logs").document(str(uuid.uuid4())).set({
        "job_id": job_id,
        "action": action,
        "details": details,
        "level": level,
        "timestamp": datetime.now(timezone.utc),
    })


# ---------------------------------------------------------------------------
# Appium connection
# ---------------------------------------------------------------------------

def _connect_appium(retries: int = 5, wait: int = 15):
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = "Pixel10"
    options.app_package = "com.google.android.gm"
    options.app_activity = ".ConversationListActivityGmail"
    options.no_reset = True
    options.full_reset = False
    options.auto_grant_permissions = True
    options.new_command_timeout = 300
    # Pixel 10 Pro display profile
    options.set_capability("avd", "Pixel10")

    server = "http://127.0.0.1:4723"
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            logger.info("Appium connect attempt %d/%d", attempt, retries)
            driver = webdriver.Remote(server, options=options)
            logger.info("Appium connected")
            return driver
        except Exception as exc:
            last_exc = exc
            logger.warning("Attempt %d failed: %s", attempt, exc)
            time.sleep(wait)
    raise RuntimeError(f"Appium connect failed after {retries} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(job_id: str) -> None:
    logger.info("=== Automation start: job %s ===", job_id)
    db = _firestore_client()

    # Load job
    doc = db.collection("jobs").document(job_id).get()
    if not doc.exists:
        raise RuntimeError(f"Job {job_id} not found in Firestore")
    job = doc.to_dict()
    user_id = job.get("user_id", "")

    # Decrypt credentials
    email = _decrypt(job["email_encrypted"])
    password = _decrypt(job["password_encrypted"])
    two_fa_key = _decrypt(job["two_fa_encrypted"])
    totp_code = _get_totp(two_fa_key)
    logger.info("Credentials decrypted for %s", email)

    db.collection("jobs").document(job_id).update({"status": "processing"})
    _log_event(db, job_id, "automation_start", "GitHub Actions emulator started")

    driver = None
    try:
        driver = _connect_appium()
        _log_event(db, job_id, "device_connected", "Appium connected to Pixel10 emulator")

        # Gmail login
        from gmail_login import GmailLogin
        GmailLogin(driver).login(email, password, totp_code)
        _log_event(db, job_id, "gmail_login", "Gmail login successful")
        logger.info("Gmail login done")

        # Google One offer link
        from google_one_automation import GoogleOneAutomation
        offer_link = GoogleOneAutomation(driver).get_offer_link()
        _log_event(db, job_id, "offer_link_found", f"Link: {offer_link}")
        logger.info("Offer link: %s", offer_link)

        # Save result
        db.collection("jobs").document(job_id).update({
            "status": "completed",
            "offer_link": offer_link,
            "completed_at": datetime.now(timezone.utc),
        })

        # Notify user
        if user_id:
            _send_telegram(user_id, f"✅ *Offer Link Ready!*\n\n🔗 {offer_link}")

        logger.info("=== Job %s completed successfully ===", job_id)

    except Exception as exc:
        logger.exception("Automation failed for job %s", job_id)
        _log_event(db, job_id, "automation_error", str(exc), level="ERROR")
        db.collection("jobs").document(job_id).update({
            "status": "failed",
            "error": str(exc),
        })
        if user_id:
            _send_telegram(user_id, f"❌ Automation failed.\nError: {exc}\n\nPlease try /start again.")
        sys.exit(1)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    main(args.job_id)
