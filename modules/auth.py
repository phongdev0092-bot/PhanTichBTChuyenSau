"""
auth.py - Xử lý đăng nhập management.mypt.vn
           + Đọc OTP tự động từ mail.fpt.net qua IMAP
"""
import imaplib
import email
import re
import time
import pickle
import os
import logging

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (EMAIL, EMAIL_PASSWORD, MANAGEMENT_URL,
                    IMAP_SERVER, IMAP_PORT, SESSION_FILE)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auth")

_driver = None
_logged_in = False

# ──────────────────────────────────────────────
#  SELENIUM DRIVER
# ──────────────────────────────────────────────
def get_driver(headless: bool = True):
    global _driver
    if _driver is not None:
        try:
            _ = _driver.title   # Kiểm tra driver còn sống
            return _driver
        except Exception:
            _driver = None

    log.info(f"Khởi động Chrome driver (headless={headless})...")
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")
    options.add_argument("--lang=vi")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    for chrome_path in ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]:
        if os.path.exists(chrome_path):
            options.binary_location = chrome_path
            break

    try:
        service = Service(ChromeDriverManager().install())
        _driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        log.warning(f"ChromeDriverManager failed ({e}), fallback to default Chrome driver...")
        _driver = webdriver.Chrome(options=options)
    _driver.set_page_load_timeout(30)
    # Ẩn navigator.webdriver để tránh bot detection
    _driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    return _driver



def close_driver():
    global _driver, _logged_in
    if _driver:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None
    _logged_in = False


# ──────────────────────────────────────────────
#  ĐỌC OTP QUA IMAP
# ──────────────────────────────────────────────
IMAP_SERVERS = [
    ("mail.fpt.net",   993, True),
    ("imap.fpt.net",   993, True),
    ("mail.fpt.net",   143, False),
]

def _get_otp_from_imap(mail_conn) -> str | None:
    mail_conn.select("INBOX")
    # Dùng tìm kiếm an toàn tiêu chuẩn để tránh lỗi encoding Unicode trong imaplib
    _, messages = mail_conn.search(None, 'ALL')
    if not messages[0]:
        return None

    uids = messages[0].split()
    # Duyệt 15 email gần nhất trong hộp thư
    for uid in reversed(uids[-15:]):
        _, msg_data = mail_conn.fetch(uid, "(RFC822)")
        if not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    try:
                        body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    except Exception:
                        pass
        else:
            try:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            except Exception:
                pass

        match = re.search(r"\b(\d{6})\b", body)
        if match:
            return match.group(1)

    return None


def get_otp_from_email(timeout: int = 75) -> str | None:
    """Thử các server IMAP để lấy OTP"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for server, port, ssl in IMAP_SERVERS:
            try:
                log.info(f"Kết nối IMAP {server}:{port} (SSL={ssl})")
                if ssl:
                    conn = imaplib.IMAP4_SSL(server, port)
                else:
                    conn = imaplib.IMAP4(server, port)
                    conn.starttls()

                conn.login(EMAIL, EMAIL_PASSWORD)
                otp = _get_otp_from_imap(conn)
                conn.logout()

                if otp:
                    log.info(f"Lấy thành công OTP từ email: {otp}")
                    return otp
                break   # Server OK nhưng chưa có email OTP - chờ lượt tới
            except Exception as e:
                log.warning(f"IMAP {server}:{port} lỗi: {e}")
                continue
        log.info("Đang chờ email OTP ... 5s")
        time.sleep(5)
    return None


# ──────────────────────────────────────────────
#  LOGIN MANAGEMENT.MYPT.VN
# ──────────────────────────────────────────────
def _try_load_session(d) -> bool:
    """Thử load session đã cache và kiểm tra phần tử thực tế trên trang"""
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        if os.path.getsize(SESSION_FILE) < 10:
            log.warning("Session file rỗng hoặc không hợp lệ")
            return False

        with open(SESSION_FILE, "rb") as f:
            cookies = pickle.load(f)

        if not cookies:
            return False

        d.get(MANAGEMENT_URL)
        time.sleep(3)
        for c in cookies:
            try:
                d.add_cookie(c)
            except Exception:
                pass
        d.refresh()
        time.sleep(4)

        # Kiểm tra xem có đang ở form login không
        login_inputs = d.find_elements(By.XPATH, '//input[@id="email" or @name="email" or contains(@placeholder, "Email")]')
        if login_inputs and any(i.is_displayed() for i in login_inputs):
            log.info("Session cache đã hết hạn (vẫn hiển thị ô đăng nhập)")
            return False

        # Kiểm tra sự xuất hiện của ô tìm kiếm hợp đồng hoặc nút Đăng xuất
        contract_inputs = d.find_elements(By.XPATH, '//input[contains(@placeholder, "hợp đồng") or contains(@placeholder, "Hợp Đồng") or contains(@placeholder, "contract")]')
        logout_btns = d.find_elements(By.XPATH, '//*[contains(text(), "Đăng xuất") or contains(text(), "Phân Tích")]')

        if (contract_inputs and any(i.is_displayed() for i in contract_inputs)) or logout_btns:
            log.info("Session cache hợp lệ (đã xác thực thành công)")
            return True

    except Exception as e:
        log.warning(f"Load session thất bại: {e}")
    return False


def _save_session(d):
    try:
        cookies = d.get_cookies()
        with open(SESSION_FILE, "wb") as f:
            pickle.dump(cookies, f)
        log.info("Đã lưu session cache thành công")
    except Exception as e:
        log.warning(f"Lưu session thất bại: {e}")


def login() -> tuple[bool, str]:
    """
    Trả về (success: bool, message: str)
    """
    global _logged_in
    d = get_driver(headless=True)

    # 1. Thử dùng session cache
    if _try_load_session(d):
        _logged_in = True
        return True, "✅ Đã sử dụng session cache hợp lệ"

    # 2. Đăng nhập mới
    log.info("Đăng nhập mới vào management.mypt.vn ...")
    try:
        d.get(f"{MANAGEMENT_URL}/login")
        time.sleep(3)
    except Exception as e:
        return False, f"❌ Không mở được trang: {e}"

    wait = WebDriverWait(d, 15)

    # --- Nhập email ---
    email_input = None
    try:
        email_input = wait.until(EC.presence_of_element_located((By.ID, "email")))
    except Exception:
        try:
            email_input = d.find_element(By.XPATH, '//input[@name="email" or @type="email" or contains(@placeholder, "Email")]')
        except NoSuchElementException:
            return False, "❌ Không tìm thấy ô nhập email"

    email_input.clear()
    email_input.send_keys(EMAIL)
    # Trigger React events
    d.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", email_input)
    d.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", email_input)
    time.sleep(1)

    # --- Bấm nút "Gửi mã OTP" ---
    clicked_otp_btn = False
    try:
        btn = d.find_element(By.XPATH, '//button[contains(.,"Gửi mã OTP") or contains(.,"Send OTP") or @type="submit"]')
        if btn.is_displayed() and btn.is_enabled():
            btn.click()
            clicked_otp_btn = True
    except Exception as e:
        log.warning(f"Lỗi bấm nút gửi OTP: {e}")

    if not clicked_otp_btn:
        return False, "❌ Nút Gửi mã OTP không khả dụng"

    log.info("Đã gửi yêu cầu OTP, chờ email gửi về ...")
    time.sleep(3)

    # --- Lấy OTP từ email ---
    otp = get_otp_from_email(timeout=75)
    if not otp:
        return False, "❌ Không lấy được OTP từ email (timeout 75s). Vui lòng kiểm tra hòm thư."

    # --- Nhập OTP ---
    otp_field = None
    try:
        otp_field = WebDriverWait(d, 10).until(
            EC.presence_of_element_located((By.XPATH, '//input[@id="otp" or @name="otp" or @placeholder="Mã OTP" or not(@id="email")]'))
        )
    except Exception:
        return False, "❌ Không tìm thấy ô nhập mã OTP"

    otp_field.clear()
    otp_field.send_keys(otp)
    d.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", otp_field)
    d.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", otp_field)
    time.sleep(1)

    # --- Bấm xác nhận đăng nhập ---
    confirm_btns = d.find_elements(By.XPATH, '//button[contains(.,"Xác nhận") or contains(.,"Đăng nhập") or contains(.,"Gửi mã OTP") or @type="submit"]')
    submitted = False
    for b in confirm_btns:
        if b.is_displayed() and b.is_enabled():
            b.click()
            submitted = True
            break

    if not submitted:
        otp_field.submit()

    time.sleep(5)

    # Kiểm tra sau khi đăng nhập
    login_inputs = d.find_elements(By.XPATH, '//input[@id="email" or @name="email"]')
    if login_inputs and any(i.is_displayed() for i in login_inputs):
        return False, "❌ Đăng nhập thất bại (vẫn ở trang đăng nhập)"

    _save_session(d)
    _logged_in = True
    log.info("Đăng nhập thành công!")
    return True, "✅ Đăng nhập hệ thống MYBAE thành công"


def is_logged_in() -> bool:
    return _logged_in
