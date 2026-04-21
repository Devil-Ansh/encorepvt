"""
run_automation.py — GitHub Actions Android emulator automation.

Flow:
  1. Spoof device model as "Pixel 10" via ADB (required for Google One offer)
  2. Set Chrome command-line flags to prevent SwiftShader GPU crash
  3. Sign in to Google via Chrome browser on the emulator (ChromeDriver bridge)
  4. Navigate to Google One app to extract the partner-eft-onboard offer link
  5. Write result to Firestore and notify the user via Telegram
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
ADB = "/usr/local/lib/android/sdk/platform-tools/adb"
DEVICE = "emulator-5554"


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
    full = f"{ADB} -s {DEVICE} {cmd}"
    try:
        result = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=timeout
        )
        out = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            logger.debug("ADB stderr (%s): %s", cmd[:40], result.stderr.strip()[:100])
        return out
    except Exception as exc:
        logger.warning("ADB error (%s): %s", cmd[:40], exc)
        return ""


def _setup_device() -> None:
    """
    Spoof device as Pixel 10 and set Chrome flags to prevent SwiftShader crash.
    Must be called after the emulator is booted.
    """
    logger.info("Setting up device as Pixel 10")

    # Root the device (works on google_apis emulator images)
    subprocess.run(f"{ADB} -s {DEVICE} root", shell=True, timeout=15)
    time.sleep(2)

    # --- Spoof device model as Pixel 10 ---
    props = {
        "ro.product.model": "Pixel 10",
        "ro.product.name": "pixel10",
        "ro.product.device": "pixel10",
        "ro.product.manufacturer": "Google",
        "ro.product.brand": "google",
        "ro.build.fingerprint": "google/pixel10/pixel10:14/AP2A.240905.003/12345678:user/release-keys",
    }
    for prop, value in props.items():
        result = _adb(f'shell setprop {prop} "{value}"')
        logger.debug("setprop %s = %s → %s", prop, value, result or "ok")

    # Verify
    model = _adb("shell getprop ro.product.model")
    logger.info("Device model reported as: %s", model)

    # --- Set Chrome command-line flags to prevent SwiftShader GPU crash ---
    # These disable GPU compositing features that crash on SwiftShader
    chrome_flags = (
        "_ "
        "--disable-gpu-compositing "
        "--disable-gpu-rasterization "
        "--in-process-gpu "
        "--disable-accelerated-2d-canvas "
        "--disable-accelerated-video-decode "
        "--disable-accelerated-video-encode "
        "--disable-webgl "
        "--disable-dev-shm-usage "
        "--disable-features=VizDisplayCompositor"
    )
    _adb(f'shell "echo \'{chrome_flags}\' > /data/local/tmp/chrome-command-line"')
    _adb("shell chmod 555 /data/local/tmp/chrome-command-line")
    verify = _adb("shell cat /data/local/tmp/chrome-command-line")
    logger.info("Chrome flags set: %s", verify[:80])

    # Kill any existing Chrome instances so flags take effect
    _adb("shell am force-stop com.android.chrome")
    time.sleep(1)


# ---------------------------------------------------------------------------
# Appium driver — Chrome mode with crash-prevention capabilities
# ---------------------------------------------------------------------------

def _connect_chrome(retries: int = 5, wait: int = 10):
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = DEVICE
    options.browser_name = "Chrome"
    options.new_command_timeout = 300

    # Pre-downloaded ChromeDriver 113 (matches emulator Chrome)
    cd_path = os.environ.get("CHROMEDRIVER_PATH", "")
    if cd_path and os.path.isfile(cd_path):
        logger.info("Using pre-downloaded ChromeDriver: %s", cd_path)
        options.set_capability("chromedriverExecutable", cd_path)
    else:
        options.set_capability("chromedriverAutodownload", True)

    # Chrome args to prevent SwiftShader crash (passed via Appium to ChromeDriver)
    options.set_capability("chromedriverArgs", [
        "--disable-gpu",
        "--disable-gpu-compositing",
        "--disable-gpu-rasterization",
        "--in-process-gpu",
        "--disable-accelerated-2d-canvas",
        "--disable-accelerated-video-decode",
        "--disable-webgl",
        "--disable-dev-shm-usage",
    ])

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
# Google sign-in via Chrome browser on emulator
# ---------------------------------------------------------------------------

def _sign_in_via_chrome(driver, email: str, password: str, totp_code: str) -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException

    wait = WebDriverWait(driver, 40)
    short_wait = WebDriverWait(driver, 15)

    logger.info("Navigating to Google sign-in")
    driver.get("https://accounts.google.com/signin/v2/identifier?hl=en&flowName=GlifWebSignIn")
    time.sleep(5)

    # --- Email ---
    logger.info("Entering email: %s", email)
    email_input = wait.until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, 'input[type="email"]')
    ))
    email_input.clear()
    email_input.send_keys(email)
    time.sleep(1)

    _click_next_web(driver)
    logger.info("Email submitted — waiting for password page")
    time.sleep(5)

    # --- Password ---
    logger.info("Entering password")
    pwd_input = wait.until(EC.presence_of_element_located(
        (By.CSS_SELECTOR, 'input[type="password"]')
    ))
    pwd_input.clear()
    pwd_input.send_keys(password)
    time.sleep(1)

    _click_next_web(driver)
    logger.info("Password submitted — waiting for next page")
    time.sleep(6)

    current_url = driver.current_url
    logger.info("Post-password URL: %s", current_url[:120])

    # --- 2FA / TOTP ---
    try:
        totp_input = short_wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR,
             'input[name="totpPin"], input[id="totpPin"], '
             'input[type="tel"], input[aria-label*="code"], '
             'input[aria-label*="Code"]')
        ))
        logger.info("2FA screen — entering TOTP code")
        totp_input.clear()
        totp_input.send_keys(totp_code)
        time.sleep(1)
        _click_next_web(driver)
        time.sleep(5)
        logger.info("TOTP submitted")
    except TimeoutException:
        logger.info("No TOTP screen — continuing")

    current_url = driver.current_url
    logger.info("Final sign-in URL: %s", current_url[:120])
    if "accounts.google.com" in current_url and ("signin" in current_url or "challenge" in current_url):
        body = ""
        try:
            body = driver.find_element(By.TAG_NAME, "body").text[:400]
        except Exception:
            pass
        raise RuntimeError(f"Google sign-in did not complete. URL: {current_url}\n{body}")
    logger.info("Google sign-in successful")


def _click_next_web(driver) -> None:
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import NoSuchElementException
    for sel in [
        '#identifierNext button',
        '#passwordNext button',
        'button[jsname="LgbsSe"]',
        '#totpNext button',
        'button[type="submit"]',
        'button[type="button"]',
    ]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            btn.click()
            return
        except NoSuchElementException:
            continue
    # JS fallback
    try:
        driver.execute_script(
            "document.querySelector('button[type=\"submit\"], "
            "button[type=\"button\"]').click()"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Google One offer extraction
# ---------------------------------------------------------------------------

def _find_offer_in_chrome(driver) -> Optional[str]:
    from selenium.webdriver.common.by import By

    logger.info("Navigating to one.google.com")
    driver.get("https://one.google.com")
    time.sleep(6)

    for scroll in range(6):
        source = driver.page_source
        match = OFFER_RE.search(source)
        if match:
            url = match.group(0).rstrip(".,;)'\"")
            logger.info("Offer link found on one.google.com: %s", url)
            return url
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)

    # Try clicking Gemini/Upgrade sections
    for text in ["Gemini Pro", "Gemini Advanced", "Upgrade", "Claim", "Benefits"]:
        try:
            els = driver.find_elements(
                By.XPATH, f'//*[contains(text(), "{text}")]'
            )
            for el in els[:3]:
                try:
                    el.click()
                    time.sleep(4)
                    match = OFFER_RE.search(driver.page_source)
                    if match:
                        return match.group(0).rstrip(".,;)'\"")
                    driver.back()
                    time.sleep(2)
                    break
                except Exception:
                    pass
        except Exception:
            pass

    return None


def _find_offer_in_native_app(driver_native) -> Optional[str]:
    from appium.webdriver.common.appiumby import AppiumBy

    PACKAGE = "com.google.android.apps.subscriptions.red"
    logger.info("Launching Google One native app")
    driver_native.activate_app(PACKAGE)
    time.sleep(6)

    for text in ["Not now", "Skip", "Maybe later", "Got it", "No thanks"]:
        try:
            driver_native.find_element(
                AppiumBy.XPATH, f'//android.widget.Button[@text="{text}"]'
            ).click()
            time.sleep(1)
        except Exception:
            pass

    for attempt in range(6):
        source = driver_native.page_source
        match = OFFER_RE.search(source)
        if match:
            return match.group(0).rstrip(".,;)'\"")

        try:
            driver_native.swipe(540, 1500, 540, 500, 800)
        except Exception:
            pass
        time.sleep(3)

        if attempt == 2:
            for tab in ["Upgrade", "Benefits", "Plans", "Gemini"]:
                try:
                    driver_native.find_element(
                        AppiumBy.XPATH, f'//android.widget.TextView[@text="{tab}"]'
                    ).click()
                    time.sleep(3)
                    break
                except Exception:
                    pass
    return None


def _connect_native_appium(retries: int = 3, wait: int = 10):
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = DEVICE
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

    email = _decrypt(job["email_encrypted"])
    password = _decrypt(job["password_encrypted"])
    two_fa_key = _decrypt(job["two_fa_encrypted"])
    totp_code = _get_totp(two_fa_key)
    logger.info("Credentials decrypted for %s", email)

    db.collection("jobs").document(job_id).update({"status": "processing"})
    _log_event(db, job_id, "automation_start", "Emulator automation started")

    # Step 1: Prepare the emulator
    _setup_device()
    _log_event(db, job_id, "device_setup", "Device spoofed as Pixel 10, Chrome flags set")

    chrome_driver = None
    native_driver = None
    offer_link = None

    try:
        # Step 2: Sign in via Chrome on the emulator
        chrome_driver = _connect_chrome()
        _sign_in_via_chrome(chrome_driver, email, password, totp_code)
        _log_event(db, job_id, "google_signin", "Signed in via Chrome on Android emulator")

        # Step 3: Try Google One web
        offer_link = _find_offer_in_chrome(chrome_driver)

        if offer_link:
            _log_event(db, job_id, "offer_found", f"Found via web: {offer_link}")
        else:
            # Step 4: Try native Google One app
            logger.info("Not found on web — trying native Google One app")
            chrome_driver.quit()
            chrome_driver = None
            time.sleep(5)

            native_driver = _connect_native_appium()
            offer_link = _find_offer_in_native_app(native_driver)
            if offer_link:
                _log_event(db, job_id, "offer_found", f"Found in native app: {offer_link}")

        if not offer_link:
            raise RuntimeError("Offer link not found in web or native Google One app")

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
