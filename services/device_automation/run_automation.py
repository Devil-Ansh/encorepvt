"""
run_automation.py — GitHub Actions Android emulator automation.

Flow:
  1. Sign in to Google via Chrome browser on the Android emulator
     (Chrome WebDriver bridge — avoids Chrome Custom Tab isolation issues)
  2. Wait for Android account manager to sync the signed-in account
  3. Open Google One native app and extract the partner-eft-onboard offer link
  4. Write result to Firestore and notify the user via Telegram
"""
import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_automation")

OFFER_RE = re.compile(
    r'https://one\.google\.com/partner-eft-onboard/[A-Za-z0-9\-._~:/?#@!$&()*+,;=%]+'
)


# ---------------------------------------------------------------------------
# Firestore helpers
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


def _log_event(db, job_id: str, action: str, details: str, level: str = "INFO") -> None:
    import uuid
    db.collection("logs").document(str(uuid.uuid4())).set({
        "job_id": job_id, "action": action, "details": details,
        "level": level, "timestamp": datetime.now(timezone.utc),
    })


# ---------------------------------------------------------------------------
# Encryption / TOTP
# ---------------------------------------------------------------------------

def _decrypt(ciphertext_b64: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = base64.b64decode(os.environ["ENCRYPTION_KEY"])
    data = base64.b64decode(ciphertext_b64)
    return AESGCM(key).decrypt(data[:12], data[12:], None).decode()


def _get_totp(secret: str) -> str:
    import pyotp
    return pyotp.TOTP(secret.strip().upper()).now()


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def _adb(cmd: str, timeout: int = 30) -> str:
    full = f"adb -s emulator-5554 {cmd}"
    try:
        result = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception as exc:
        logger.warning("ADB error (%s): %s", cmd, exc)
        return ""


def _wait_for_activity(package: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = _adb("shell dumpsys activity | grep mResumedActivity")
        if package in out:
            return True
        time.sleep(2)
    return False


def _enable_chrome_debugging() -> None:
    """Enable remote debugging in Chrome on the emulator."""
    _adb("shell am set-debug-app --persistent com.android.chrome")
    _adb('shell am start -n com.android.chrome/com.google.android.apps.chrome.Main '
         '-a android.intent.action.VIEW -d "about:blank"')
    time.sleep(3)


# ---------------------------------------------------------------------------
# Appium driver factory — Chrome mode
# ---------------------------------------------------------------------------

def _connect_chrome(retries: int = 4, wait: int = 10):
    """Connect Appium to Chrome browser on the emulator via ChromeDriver."""
    from appium import webdriver
    from appium.options.android import ChromeOptions

    options = ChromeOptions()
    options.platform_name = "Android"
    options.device_name = "emulator-5554"
    options.browser_name = "Chrome"
    options.set_capability("chromedriverAutodownload", True)
    options.new_command_timeout = 300

    server = "http://127.0.0.1:4723"
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            logger.info("Connecting Appium/Chrome (attempt %d/%d)", attempt, retries)
            driver = webdriver.Remote(server, options=options)
            logger.info("Appium Chrome connected")
            return driver
        except Exception as exc:
            last_exc = exc
            logger.warning("Attempt %d failed: %s", attempt, exc)
            time.sleep(wait)
    raise RuntimeError(f"Chrome connect failed after {retries} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Google sign-in via Chrome browser
# ---------------------------------------------------------------------------

def _sign_in_via_chrome(driver, email: str, password: str, totp_code: str) -> None:
    """
    Sign in to Google account using Chrome browser on the Android emulator.
    Uses standard Selenium CSS selectors (not native Android elements).
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException

    wait = WebDriverWait(driver, 30)
    short_wait = WebDriverWait(driver, 10)

    logger.info("Navigating to Google sign-in")
    driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
    time.sleep(3)

    # --- Email field ---
    logger.info("Entering email")
    email_input = wait.until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, 'input[type="email"], input[name="identifier"], #identifierId')
    ))
    email_input.clear()
    email_input.send_keys(email)
    time.sleep(1)

    # Click Next
    for sel in ['#identifierNext', 'button[jsname="LgbsSe"]', 'button[type="button"]']:
        try:
            driver.find_element(By.CSS_SELECTOR, sel).click()
            break
        except NoSuchElementException:
            continue
    time.sleep(3)

    # --- Password field ---
    logger.info("Entering password")
    pwd_input = wait.until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, 'input[type="password"], input[name="password"], #password input')
    ))
    pwd_input.clear()
    pwd_input.send_keys(password)
    time.sleep(1)

    for sel in ['#passwordNext', 'button[jsname="LgbsSe"]', 'button[type="button"]']:
        try:
            driver.find_element(By.CSS_SELECTOR, sel).click()
            break
        except NoSuchElementException:
            continue
    time.sleep(4)

    # --- 2FA / TOTP ---
    try:
        totp_input = short_wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, 'input[name="totpPin"], input[id="totpPin"], input[type="tel"]')
        ))
        logger.info("2FA screen — entering TOTP")
        totp_input.clear()
        totp_input.send_keys(totp_code)
        time.sleep(1)
        for sel in ['button[jsname="LgbsSe"]', '#totpNext', 'button[type="button"]']:
            try:
                driver.find_element(By.CSS_SELECTOR, sel).click()
                break
            except NoSuchElementException:
                continue
        time.sleep(3)
    except TimeoutException:
        logger.info("No 2FA screen detected")

    # Verify signed in by checking current URL
    current_url = driver.current_url
    logger.info("Post-login URL: %s", current_url)
    if "accounts.google.com" in current_url and "signin" in current_url:
        # Still on sign-in page — login may have failed
        page_text = driver.find_element(By.TAG_NAME, "body").text[:300]
        raise RuntimeError(f"Google sign-in failed. Page: {page_text}")
    logger.info("Google sign-in successful")


# ---------------------------------------------------------------------------
# Google One offer link extraction
# ---------------------------------------------------------------------------

def _extract_offer_from_chrome(driver) -> Optional[str]:
    """Try to find the offer link on one.google.com via Chrome."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    logger.info("Opening one.google.com in Chrome")
    driver.get("https://one.google.com")
    time.sleep(5)

    # Look for the offer link in page source
    for _ in range(6):
        source = driver.page_source
        match = OFFER_RE.search(source)
        if match:
            url = match.group(0).rstrip(".,;)")
            logger.info("Found offer link in Chrome: %s", url)
            return url
        # Try scrolling to load more content
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)

    # Try clicking on upgrade/Gemini sections
    for text in ["Gemini Pro", "Gemini Advanced", "Upgrade", "Benefits", "Claim"]:
        try:
            els = driver.find_elements(By.XPATH, f'//*[contains(text(), "{text}")]')
            for el in els:
                try:
                    el.click()
                    time.sleep(3)
                    source = driver.page_source
                    match = OFFER_RE.search(source)
                    if match:
                        return match.group(0).rstrip(".,;)")
                    driver.get("https://one.google.com")
                    time.sleep(4)
                    break
                except Exception:
                    pass
        except Exception:
            pass

    return None


def _extract_offer_from_native_app(driver_native) -> Optional[str]:
    """Open Google One native app and look for the offer link."""
    from appium.webdriver.common.appiumby import AppiumBy

    PACKAGE = "com.google.android.apps.subscriptions.red"
    logger.info("Launching Google One native app")
    driver_native.activate_app(PACKAGE)
    time.sleep(5)

    # Dismiss any dialogs
    for text in ["Not now", "Skip", "Maybe later", "Got it", "No thanks"]:
        try:
            driver_native.find_element(
                AppiumBy.XPATH, f'//android.widget.Button[@text="{text}"]'
            ).click()
            time.sleep(1)
        except Exception:
            pass

    # Search page source and element tree for the offer URL
    for attempt in range(5):
        source = driver_native.page_source
        match = OFFER_RE.search(source)
        if match:
            return match.group(0).rstrip(".,;)")

        # Try scrolling
        try:
            driver_native.swipe(540, 1500, 540, 500, 800)
        except Exception:
            pass
        time.sleep(3)

        # Try navigating to different tabs
        if attempt == 2:
            for tab in ["Upgrade", "Benefits", "Plans"]:
                try:
                    driver_native.find_element(
                        AppiumBy.XPATH, f'//android.widget.TextView[@text="{tab}"]'
                    ).click()
                    time.sleep(2)
                    break
                except Exception:
                    pass

    return None


def _connect_native_appium(retries: int = 3, wait: int = 10):
    """Connect to the native Android UiAutomator2 driver."""
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = "emulator-5554"
    options.no_reset = True
    options.full_reset = False
    options.auto_grant_permissions = True
    options.new_command_timeout = 300

    server = "http://127.0.0.1:4723"
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            logger.info("Native Appium connect attempt %d/%d", attempt, retries)
            driver = webdriver.Remote(server, options=options)
            logger.info("Native Appium connected")
            return driver
        except Exception as exc:
            last_exc = exc
            logger.warning("Attempt %d failed: %s", attempt, exc)
            time.sleep(wait)
    raise RuntimeError(f"Native Appium connect failed: {last_exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(job_id: str) -> None:
    logger.info("=== Automation start: job %s ===", job_id)

    db = _firestore_client()
    doc = db.collection("jobs").document(job_id).get()
    if not doc.exists:
        raise RuntimeError(f"Job {job_id} not found")
    job = doc.to_dict()
    user_id = job.get("user_id", "")

    # Decrypt credentials
    email = _decrypt(job["email_encrypted"])
    password = _decrypt(job["password_encrypted"])
    two_fa_key = _decrypt(job["two_fa_encrypted"])
    totp_code = _get_totp(two_fa_key)
    logger.info("Credentials decrypted for %s", email)

    db.collection("jobs").document(job_id).update({"status": "processing"})
    _log_event(db, job_id, "automation_start", "Emulator automation started")

    # Enable Chrome debugging on emulator
    _enable_chrome_debugging()

    chrome_driver = None
    native_driver = None
    offer_link = None

    try:
        # Step 1: Sign in via Chrome browser
        chrome_driver = _connect_chrome()
        _sign_in_via_chrome(chrome_driver, email, password, totp_code)
        _log_event(db, job_id, "google_signin", "Signed in via Chrome browser")

        # Step 2: Try to get offer link from one.google.com in Chrome
        offer_link = _extract_offer_from_chrome(chrome_driver)

        if offer_link:
            _log_event(db, job_id, "offer_link_found", f"Found in Chrome: {offer_link}")
        else:
            # Step 3: Sync account to device and try native Google One app
            logger.info("Offer not found in Chrome — trying native app after account sync")
            chrome_driver.quit()
            chrome_driver = None

            # Wait for account to sync to Android account manager
            time.sleep(10)

            native_driver = _connect_native_appium()
            offer_link = _extract_offer_from_native_app(native_driver)
            if offer_link:
                _log_event(db, job_id, "offer_link_found", f"Found in native app: {offer_link}")

        if not offer_link:
            raise RuntimeError("Offer link not found in Chrome or native Google One app")

        # Save result
        db.collection("jobs").document(job_id).update({
            "status": "completed",
            "offer_link": offer_link,
            "completed_at": datetime.now(timezone.utc),
        })

        if user_id:
            _send_telegram(user_id, f"✅ *Offer Link Ready!*\n\n🔗 {offer_link}")

        logger.info("=== Job %s completed: %s ===", job_id, offer_link)

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
        for d in [chrome_driver, native_driver]:
            if d:
                try:
                    d.quit()
                except Exception:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    main(args.job_id)
