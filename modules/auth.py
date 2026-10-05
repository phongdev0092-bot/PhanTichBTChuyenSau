"""
auth.py - Xử lý đăng nhập management.mypt.vn
           + Đọc OTP tự động từ mail.fpt.net qua IMAP (hoặc nhập OTP thủ công)
           + Quản lý Chrome WebDriver an toàn, chống rò rỉ tiến trình, chống treo ứng dụng
"""
import imaplib
import email
import re
import time
import pickle
import os
import sys
import logging
import threading

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (EMAIL, EMAIL_PASSWORD, MANAGEMENT_URL,
                    IMAP_SERVER, IMAP_PORT, SESSION_FILE)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auth")

_driver = None
_logged_in = False
_login_lock = threading.Lock()


# ──────────────────────────────────────────────
#  SELENIUM DRIVER QUẢN LÝ AN TOÀN
# ──────────────────────────────────────────────
def get_driver(headless: bool = True, force_new: bool = False):
    """
    Lấy instance Chrome Driver hiện tại hoặc khởi tạo mới nếu chưa có / bị chết.
    Nếu force_new=True, đóng driver cũ hoàn toàn trước khi tạo mới.
    """
    global _driver
    if force_new:
        close_driver()

    if _driver is not None:
        try:
            # Kiểm tra driver còn phản hồi không
            _ = _driver.title
            return _driver
        except Exception as e:
            log.warning(f"Driver cũ mất phản hồi ({e}), dọn dẹp khởi động lại...")
            close_driver()

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
    options.add_argument("--remote-debugging-pipe")
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
    try:
        _driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        })
    except Exception:
        pass

    return _driver


def close_driver():
    """Đóng sạch sẽ instance Selenium driver và giải phóng tiến trình."""
    global _driver, _logged_in
    if _driver:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None
    _logged_in = False
    log.info("Đã đóng và giải phóng Selenium driver.")


def force_kill_driver():
    """Dọn dẹp cưỡng chế driver và tài nguyên."""
    close_driver()


# ──────────────────────────────────────────────
#  ĐỌC OTP QUA IMAP AN TOÀN & THÔNG MINH
# ──────────────────────────────────────────────
def _get_otp_from_imap(mail_conn) -> str | None:
    """Đọc hòm thư INBOX, lấy mã OTP 6 số từ tối đa 5 email mới nhất (nhanh, nhẹ)."""
    try:
        mail_conn.select("INBOX", readonly=True)
        # Tìm các email gần nhất
        _, messages = mail_conn.search(None, 'ALL')
        if not messages or not messages[0]:
            return None

        uids = messages[0].split()
        # Chỉ quét 5 email mới nhất để tối ưu tốc độ và không tải attachments nặng
        for uid in reversed(uids[-5:]):
            try:
                # Tải phần text và header, tránh tải file đính kèm lớn
                _, msg_data = mail_conn.fetch(uid, "(RFC822.HEADER BODY.PEEK[TEXT])")
                if not msg_data or not msg_data[0]:
                    # fallback fetch RFC822 nhẹ
                    _, msg_data = mail_conn.fetch(uid, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue

                raw_email = b""
                for part in msg_data:
                    if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes):
                        raw_email += part[1]

                if not raw_email:
                    continue

                msg = email.message_from_bytes(raw_email)
                subject = msg.get("Subject", "")
                
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

                full_text = f"{subject}\n{body}"
                # Tìm OTP 6 chữ số
                # Ưu tiên tìm trong văn cảnh có chữ OTP / mã xác nhận / MyPT
                m_context = re.search(r"(?:mã|otp|code|xác nhận)[^\d]{1,25}(\b\d{6}\b)", full_text, re.IGNORECASE)
                if m_context:
                    return m_context.group(1)

                m_simple = re.search(r"\b(\d{6})\b", full_text)
                if m_simple:
                    return m_simple.group(1)
            except Exception as e:
                log.debug(f"Bỏ qua email uid {uid} do lỗi đọc: {e}")
                continue

    except Exception as e:
        log.warning(f"Lỗi truy vấn INBOX IMAP: {e}")
    return None


def get_otp_from_email(email_addr: str = None, email_pass: str = None, timeout: int = 45, step_callback=None) -> tuple[str | None, str]:
    """
    Thử kết nối IMAP mail.fpt.net (port 993, SSL) với socket timeout chuẩn (10s).
    Nhận diện ngay lập tức lỗi sai mật khẩu để không bị treo.
    Trả về: (otp_str, status_msg)
    """
    mail_user = (email_addr or EMAIL).strip()
    mail_pwd  = (email_pass or EMAIL_PASSWORD).strip()

    if not mail_user or not mail_pwd:
        return None, "Thiếu tài khoản hoặc mật khẩu email MyPT/FPT"

    server = IMAP_SERVER or "mail.fpt.net"
    port = int(IMAP_PORT or 993)
    deadline = time.time() + timeout
    attempt = 0

    # Chuẩn bị các biến thể username: cả email và tên trước @
    usernames_to_try = [mail_user]
    if "@" in mail_user:
        usernames_to_try.append(mail_user.split("@")[0])

    while time.time() < deadline:
        attempt += 1
        msg_progress = f"Đang kết nối hòm thư {server} tìm mã OTP (lần {attempt})..."
        log.info(msg_progress)
        if step_callback:
            step_callback(msg_progress)

        auth_failed_count = 0
        conn_ok = False

        for user_cand in usernames_to_try:
            conn = None
            try:
                # Socket timeout 10 giây chặt chẽ tránh treo vĩnh viễn
                conn = imaplib.IMAP4_SSL(server, port, timeout=10)
                conn.login(user_cand, mail_pwd)
                conn_ok = True
                
                otp = _get_otp_from_imap(conn)
                try:
                    conn.logout()
                except Exception:
                    pass

                if otp:
                    log.info(f"Lấy thành công mã OTP: {otp}")
                    return otp, "Lấy OTP thành công"
                
                # Đăng nhập thành công nhưng chưa có email OTP
                break

            except imaplib.IMAP4.error as imap_err:
                err_str = str(imap_err).lower()
                if "authenticationfailed" in err_str or "login failed" in err_str or "invalid credentials" in err_str:
                    auth_failed_count += 1
                log.warning(f"IMAP login với {user_cand} thất bại: {imap_err}")
            except Exception as e:
                log.warning(f"IMAP {server}:{port} lỗi kết nối: {e}")
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        # Nếu tất cả các cách thử user đều bị authentication failed: dừng ngay, không loop vô nghĩa!
        if auth_failed_count >= len(usernames_to_try):
            err_msg = f"❌ Sai thông tin đăng nhập Email FPT (IMAP): Mật khẩu hoặc tài khoản '{mail_user}' không đúng."
            log.error(err_msg)
            return None, err_msg

        remaining = int(deadline - time.time())
        if remaining > 0:
            time.sleep(min(3, remaining))

    return None, f"❌ Hết thời gian chờ nhận mã OTP từ hòm thư ({timeout}s). Vui lòng kiểm tra hộp thư hoặc chọn Nhập OTP thủ công."


# ──────────────────────────────────────────────
#  LOGIN MANAGEMENT.MYPT.VN
# ──────────────────────────────────────────────
def _try_load_session(d) -> bool:
    """Thử load session đã cache (cookies + localStorage) và kiểm tra phần tử thực tế trên trang."""
    global _logged_in
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        if os.path.getsize(SESSION_FILE) < 10:
            log.warning("Session file rỗng hoặc không hợp lệ")
            return False

        with open(SESSION_FILE, "rb") as f:
            cache_data = pickle.load(f)

        cookies = []
        local_storage = {}
        if isinstance(cache_data, dict):
            cookies = cache_data.get("cookies", [])
            local_storage = cache_data.get("local_storage", {})
        elif isinstance(cache_data, list):
            cookies = cache_data

        if not cookies and not local_storage:
            return False

        d.get(MANAGEMENT_URL)
        time.sleep(2)
        for c in cookies:
            try:
                d.add_cookie(c)
            except Exception:
                pass

        if local_storage:
            for k, v in local_storage.items():
                try:
                    d.execute_script("window.localStorage.setItem(arguments[0], arguments[1]);", k, v)
                except Exception:
                    pass

        # Điều hướng trực tiếp đến trang check-contract để xác minh quyền truy cập
        d.get(f"{MANAGEMENT_URL}/check-contract")
        time.sleep(3)

        # Kiểm tra nếu bị redirect sang trang login -> session hết hạn
        if "/login" in d.current_url:
            log.info("Session cache đã hết hạn (bị chuyển hướng về trang /login)")
            _logged_in = False
            return False

        login_inputs = d.find_elements(By.XPATH, '//input[@id="email" or @name="email" or contains(@placeholder, "Email")]')
        if login_inputs and any(i.is_displayed() for i in login_inputs):
            log.info("Session cache đã hết hạn (vẫn hiển thị ô đăng nhập)")
            _logged_in = False
            return False

        # Kiểm tra sự xuất hiện của ô tìm kiếm hợp đồng, nút Phân Tích hoặc URL /check-contract
        contract_inputs = d.find_elements(By.XPATH, '//input[@id="contract" or contains(@placeholder, "hợp đồng") or contains(@placeholder, "Hợp Đồng") or contains(@placeholder, "contract")]')
        phanthich_btns = d.find_elements(By.XPATH, '//button[contains(normalize-space(.), "Phân tích") or contains(normalize-space(.), "Phân Tích")]')

        if (contract_inputs and any(i.is_displayed() for i in contract_inputs)) or phanthich_btns or "/check-contract" in d.current_url:
            log.info("Session cache hợp lệ (đã xác thực thành công vào check-contract)")
            _logged_in = True
            return True

    except Exception as e:
        log.warning(f"Load session thất bại: {e}")
    return False


def _save_session(d):
    """Lưu toàn bộ cookies và localStorage vào session cache."""
    try:
        cookies = d.get_cookies()
        local_storage = {}
        try:
            local_storage = d.execute_script("return Object.assign({}, window.localStorage);") or {}
        except Exception:
            pass

        if cookies or local_storage:
            cache_data = {
                "cookies": cookies,
                "local_storage": local_storage,
                "saved_at": time.time()
            }
            with open(SESSION_FILE, "wb") as f:
                pickle.dump(cache_data, f)
            log.info(f"Đã lưu session cache thành công ({len(cookies)} cookies, {len(local_storage)} localStorage items)")
        else:
            log.warning("Không có cookies hoặc localStorage từ trình duyệt, bỏ qua việc ghi session.pkl")
    except Exception as e:
        log.warning(f"Lưu session thất bại: {e}")



def _set_react_input(d, input_elem, value: str):
    """Gán giá trị vào ô input của React kèm trigger event chuẩn 100%."""
    d.execute_script("""
        const input = arguments[0];
        const val = arguments[1];
        const lastValue = input.value;
        input.value = val;
        const tracker = input._valueTracker;
        if (tracker) {
            tracker.setValue(lastValue);
        }
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        input.dispatchEvent(new Event('blur', { bubbles: true }));
    """, input_elem, value)


def login(email_addr: str = None, email_pass: str = None, step_callback=None, otp_method: str = "auto") -> tuple[bool, str]:
    """
    Đăng nhập vào management.mypt.vn.
    Hỗ trợ 2 phương thức OTP:
      - 'auto': Tự động đọc OTP từ hòm mail IMAP mail.fpt.net
      - 'manual': Điền email, gửi OTP, chuyển sang trạng thái chờ user gửi OTP lên
    Trả về (success: bool, message: str)
    """
    global _logged_in
    target_email = (email_addr or EMAIL).strip()
    target_pwd   = (email_pass or EMAIL_PASSWORD).strip()

    with _login_lock:
        try:
            if step_callback:
                step_callback("Đang khởi tạo trình duyệt Chrome an toàn...")
            
            # Khởi tạo driver mới sạch sẽ để tránh xung đột session
            d = get_driver(headless=True, force_new=True)

            # 1. Thử dùng session cache (chỉ nếu dùng tài khoản mặc định và không ép login lại)
            if not email_addr and _try_load_session(d):
                _logged_in = True
                return True, "✅ Đã sử dụng session cache hợp lệ"

            # 2. Mở trang đăng nhập
            if step_callback:
                step_callback(f"Đang mở trang đăng nhập MyPT ({MANAGEMENT_URL}/login)...")
            log.info(f"Đăng nhập mới vào management.mypt.vn với tài khoản {target_email} ...")
            
            try:
                d.get(f"{MANAGEMENT_URL}/login")
                time.sleep(2)
            except Exception as e:
                force_kill_driver()
                return False, f"❌ Không mở được trang đăng nhập: {e}"

            wait = WebDriverWait(d, 15)

            # --- Nhập email ---
            email_input = None
            try:
                email_input = wait.until(EC.presence_of_element_located((By.ID, "email")))
            except Exception:
                try:
                    email_input = d.find_element(By.XPATH, '//input[@name="email" or @type="email" or contains(@placeholder, "Email")]')
                except NoSuchElementException:
                    force_kill_driver()
                    return False, "❌ Không tìm thấy ô nhập email trên trang"

            email_input.clear()
            _set_react_input(d, email_input, target_email)
            email_input.send_keys(target_email)
            _set_react_input(d, email_input, target_email)
            time.sleep(1)

            # --- Bấm nút "Gửi mã OTP" ---
            if step_callback:
                step_callback(f"Đang gửi yêu cầu mã OTP cho email {target_email}...")

            clicked_otp_btn = False
            for _ in range(5):
                try:
                    btn = d.find_element(By.XPATH, '//button[contains(.,"Gửi mã OTP") or contains(.,"Send OTP") or @type="submit"]')
                    if btn.is_displayed():
                        if btn.is_enabled():
                            btn.click()
                            clicked_otp_btn = True
                            break
                        else:
                            # Re-trigger React change nếu nút chưa enable
                            _set_react_input(d, email_input, target_email)
                            time.sleep(0.5)
                except Exception:
                    time.sleep(0.5)

            if not clicked_otp_btn:
                # Cố gắng click qua JavaScript
                try:
                    btn = d.find_element(By.XPATH, '//button[contains(.,"Gửi mã OTP") or contains(.,"Send OTP") or @type="submit"]')
                    d.execute_script("arguments[0].click();", btn)
                    clicked_otp_btn = True
                except Exception:
                    pass

            if not clicked_otp_btn:
                force_kill_driver()
                return False, "❌ Nút 'Gửi mã OTP' không khả dụng hoặc bị khóa trên trang."

            log.info("Đã bấm gửi yêu cầu OTP, kiểm tra phản hồi từ web...")
            time.sleep(2)

            # Kiểm tra xem có alert báo lỗi email không tồn tại hoặc lỗi khác không
            error_alerts = d.find_elements(By.XPATH, '//*[contains(@class, "alert") or contains(@class, "error") or contains(@class, "toast")][contains(text(), "lỗi") or contains(text(), "không tồn tại") or contains(text(), "thất bại")]')
            for a in error_alerts:
                if a.is_displayed() and a.text.strip():
                    force_kill_driver()
                    return False, f"❌ Thông báo từ hệ thống: {a.text.strip()}"

            # --- Nếu chế độ là nhập OTP thủ công ---
            if otp_method == "manual":
                if step_callback:
                    step_callback("Đã gửi mã OTP! Vui lòng nhập mã OTP 6 số vừa nhận được vào ô bên dưới.")
                # Trả về tín hiệu waiting_otp để frontend hiển thị prompt/modal nhập OTP
                return True, "waiting_otp"

            # --- Chế độ tự động đọc OTP qua Email IMAP ---
            if step_callback:
                step_callback(f"Đã gửi yêu cầu OTP. Đang chờ mã gửi về hòm mail {target_email}...")

            otp, otp_msg = get_otp_from_email(email_addr=target_email, email_pass=target_pwd, timeout=45, step_callback=step_callback)
            if not otp:
                force_kill_driver()
                return False, otp_msg

            # --- Điền mã OTP và xác nhận ---
            if step_callback:
                step_callback(f"Đã nhận mã OTP ({otp}). Đang tiến hành xác thực đăng nhập...")

            return _enter_otp_and_finish(d, otp, step_callback)

        except Exception as e:
            log.error(f"Ngoại lệ khi đăng nhập: {e}", exc_info=True)
            force_kill_driver()
            return False, f"❌ Lỗi xử lý đăng nhập: {str(e)}"


def _enter_otp_and_finish(d, otp: str, step_callback=None) -> tuple[bool, str]:
    """Nhập mã OTP 6 số vào form và hoàn tất đăng nhập."""
    global _logged_in
    try:
        otp_field = None
        try:
            otp_field = WebDriverWait(d, 12).until(
                EC.presence_of_element_located((By.XPATH, '//input[@id="otp" or @name="otp" or contains(@placeholder, "OTP") or contains(@placeholder, "mã") or not(@id="email")]'))
            )
        except Exception:
            force_kill_driver()
            return False, "❌ Không tìm thấy ô nhập mã OTP trên trang."

        otp_field.clear()
        _set_react_input(d, otp_field, otp)
        otp_field.send_keys(otp)
        _set_react_input(d, otp_field, otp)
        time.sleep(1)

        # Bấm nút xác nhận
        confirm_btns = d.find_elements(By.XPATH, '//button[contains(.,"Xác nhận") or contains(.,"Đăng nhập") or @type="submit"]')
        submitted = False
        for b in confirm_btns:
            if b.is_displayed() and b.is_enabled():
                b.click()
                submitted = True
                break

        if not submitted:
            try:
                otp_field.submit()
            except Exception:
                d.execute_script("arguments[0].click();", confirm_btns[0] if confirm_btns else otp_field)

        if step_callback:
            step_callback("Đang kiểm tra kết quả xác thực...")
        time.sleep(4)

        # Kiểm tra xem có còn ở trang login hay báo lỗi không
        login_inputs = d.find_elements(By.XPATH, '//input[@id="email" or @name="email"]')
        if login_inputs and any(i.is_displayed() for i in login_inputs):
            # Tìm thông báo lỗi nếu có
            err_box = d.find_elements(By.XPATH, '//*[contains(@class, "error") or contains(@class, "alert") or contains(@class, "toast")]')
            err_txt = "Đăng nhập thất bại (Mã OTP có thể đã hết hạn hoặc không đúng)"
            for eb in err_box:
                if eb.is_displayed() and eb.text.strip():
                    err_txt = eb.text.strip()
                    break
            force_kill_driver()
            return False, f"❌ {err_txt}"

        _save_session(d)
        _logged_in = True
        log.info("Đăng nhập thành công!")
        return True, "✅ Đăng nhập hệ thống MYBAE thành công"

    except Exception as e:
        log.error(f"Lỗi nhập OTP: {e}")
        force_kill_driver()
        return False, f"❌ Lỗi xác nhận OTP: {e}"


def submit_manual_otp(otp: str, step_callback=None) -> tuple[bool, str]:
    """Nhập OTP do người dùng nhập tay vào trình duyệt đang mở."""
    global _driver
    with _login_lock:
        if not _driver:
            return False, "❌ Phiên trình duyệt đã đóng hoặc chưa khởi tạo. Vui lòng bấm Đăng nhập lại."
        try:
            return _enter_otp_and_finish(_driver, otp.strip(), step_callback)
        except Exception as e:
            force_kill_driver()
            return False, f"❌ Lỗi nhập OTP thủ công: {e}"


def is_logged_in() -> bool:
    return _logged_in
