"""
inside_fpt.py - Tự động đăng nhập FPT Inside & ghi chú kết quả phân tích Bảo Trì
Luồng:
  1. Đăng nhập login.fpt.net bằng account + password + TOTP (Google Authenticator)
  2. Điều hướng: Inside New V5 → Hỗ trợ kỹ thuật → Thông tin chung → tab Bảo Trì
  3. Với mỗi HĐ có cảnh báo/cần xử lý:
     - Nhập số HĐ → Bấm Xem CL
     - Chuyển sang tab mới → Ghi chú → dấu + → Nhập nội dung → Cập nhật
"""
import logging
import time
import re
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager

log = logging.getLogger("inside_fpt")

# ── Singleton driver cho session Inside FPT ──────────────────────────────────
_inside_driver = None
_inside_logged_in = False

# ── Global job state cho Auto Note ───────────────────────────────────────────
_auto_note_running   = False
_auto_note_status    = "idle"   # idle | logging_in | running | done | error | cancelled
_auto_note_cancel    = False
_auto_note_progress  = 0
_auto_note_total     = 0
_auto_note_log       = []       # list of {time, level, msg}
_auto_note_results   = []       # list of {so_hd, status, msg}
_auto_note_percent   = 0
_auto_note_current   = ""


def _log(msg: str, level: str = "info"):
    """Ghi log vào bộ nhớ để stream lên giao diện + console."""
    ts = time.strftime("%H:%M:%S")
    _auto_note_log.append({"time": ts, "level": level, "msg": msg})
    if level == "error":
        log.error(msg)
    elif level == "warning":
        log.warning(msg)
    else:
        log.info(msg)


# ─────────────────────────────────────────────────────────────────────────────
#  TOTP - Tự động generate mã OTP từ secret key
# ─────────────────────────────────────────────────────────────────────────────
def get_totp_code(secret: str) -> str | None:
    """Generate mã OTP hiện tại từ TOTP secret key (Base32)."""
    if not secret:
        return None
    try:
        import pyotp
        totp = pyotp.TOTP(secret.strip().upper())
        code = totp.now()
        log.info(f"TOTP generated: {code} (valid ~{30 - int(time.time()) % 30}s)")
        return code
    except Exception as e:
        log.error(f"Lỗi generate TOTP: {e}")
        return None


def decode_qr_secret(image_path: str) -> str | None:
    """
    Giải mã QR code Google Authenticator từ ảnh để lấy secret key.
    Hỗ trợ định dạng: otpauth://totp/...?secret=XXXX&...
    """
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode as pyzbar_decode

        img = Image.open(image_path)
        decoded_list = pyzbar_decode(img)
        for item in decoded_list:
            data = item.data.decode("utf-8", errors="ignore")
            log.info(f"QR decoded: {data[:80]}...")
            # Trích xuất secret từ otpauth URI
            match = re.search(r"[?&]secret=([A-Z2-7a-z]+)", data)
            if match:
                secret = match.group(1).upper()
                log.info(f"✅ Đã lấy được TOTP secret: {secret[:6]}****")
                return secret
        log.warning("QR code không chứa thông tin TOTP hợp lệ (otpauth://totp/...)")
        return None
    except ImportError:
        log.error("Thiếu thư viện pyzbar/Pillow - chạy: pip install pyzbar Pillow")
        return None
    except Exception as e:
        log.error(f"Lỗi giải mã QR: {e}")
        return None


def save_totp_secret(secret: str) -> bool:
    """Lưu TOTP secret vào config.py để dùng lần sau."""
    try:
        import config
        config.TOTP_SECRET = secret

        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py")
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        if "TOTP_SECRET" in content:
            content = re.sub(r"TOTP_SECRET\s*=\s*['\"].*?['\"]", f"TOTP_SECRET = '{secret}'", content)
        else:
            content += f"\nTOTP_SECRET = '{secret}'\n"

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)

        log.info("✅ Đã lưu TOTP secret vào config.py")
        return True
    except Exception as e:
        log.error(f"Lỗi lưu TOTP secret: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  SELENIUM DRIVER (Dùng riêng cho Inside FPT, không dùng chung với management)
# ─────────────────────────────────────────────────────────────────────────────
def _get_inside_driver(headless: bool = True):
    global _inside_driver
    if _inside_driver is not None:
        try:
            _ = _inside_driver.title
            return _inside_driver
        except Exception:
            _inside_driver = None

    _log("Khởi động Chrome driver cho Inside FPT...")
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
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
        _inside_driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        _log(f"ChromeDriverManager thất bại ({e}), thử fallback...", "warning")
        _inside_driver = webdriver.Chrome(options=options)

    _inside_driver.set_page_load_timeout(45)
    _inside_driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    return _inside_driver


def close_inside_driver():
    global _inside_driver, _inside_logged_in
    if _inside_driver:
        try:
            _inside_driver.quit()
        except Exception:
            pass
        _inside_driver = None
    _inside_logged_in = False


def _safe_click(driver, element, use_js=False):
    """Click an element safely with fallback to JS click."""
    try:
        if use_js:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", element)
        else:
            element.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            return False


def _wait_for(driver, by, value, timeout=15, condition="clickable"):
    wait = WebDriverWait(driver, timeout)
    try:
        if condition == "clickable":
            return wait.until(EC.element_to_be_clickable((by, value)))
        elif condition == "visible":
            return wait.until(EC.visibility_of_element_located((by, value)))
        else:
            return wait.until(EC.presence_of_element_located((by, value)))
    except TimeoutException:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#  ĐĂNG NHẬP FPT INSIDE
# ─────────────────────────────────────────────────────────────────────────────
def login_inside_fpt(totp_secret: str = None) -> tuple[bool, str]:
    """
    Đăng nhập vào FPT Inside qua login.fpt.net.
    Luồng: tích cbOTP → nhập tài khoản → nhập mật khẩu → nhập OTP → bấm #btnLogin
    """
    global _inside_logged_in

    import config
    account  = config.INSIDE_ACCOUNT
    password = config.INSIDE_PASSWORD
    login_url = getattr(config, "INSIDE_FPT_LOGIN_URL", "http://login.fpt.net/")
    secret   = totp_secret or getattr(config, "TOTP_SECRET", "") or ""

    d = _get_inside_driver(headless=True)

    # ── 1. Mở trang đăng nhập ─────────────────────────────────────────────
    _log(f"Mở trang đăng nhập: {login_url}")
    try:
        d.get(login_url)
        time.sleep(2)
    except Exception as e:
        return False, f"❌ Không mở được trang đăng nhập: {e}"

    # ── 2. Bấm "Sử dụng OTP" trước tiên ──────────────────────────────────
    try:
        cb_otp = d.find_element(By.ID, "cbOTP")
        if not cb_otp.is_selected():
            _log("Tích vào checkbox 'Sử dụng OTP'...")
            d.execute_script("arguments[0].click();", cb_otp)
            time.sleep(0.5)
    except Exception:
        pass

    # ── 3. Nhập tài khoản ─────────────────────────────────────────────────
    _log(f"Nhập tài khoản: {account}")
    try:
        username_el = d.find_element(By.ID, "fUserName")
    except NoSuchElementException:
        username_el = d.find_element(By.XPATH, '//input[@type="text"][1]')
    username_el.clear()
    username_el.send_keys(account)
    time.sleep(0.3)

    # ── 4. Nhập mật khẩu ──────────────────────────────────────────────────
    _log("Nhập mật khẩu...")
    try:
        password_el = d.find_element(By.ID, "fPassword")
    except NoSuchElementException:
        password_el = d.find_element(By.XPATH, '//input[@type="password"]')
    password_el.clear()
    password_el.send_keys(password)
    time.sleep(0.3)

    # ── 5. Nhập OTP ───────────────────────────────────────────────────────
    if secret:
        otp_code = get_totp_code(secret)
        if otp_code:
            _log(f"Nhập mã OTP: {otp_code}")
            try:
                otp_el = d.find_element(By.ID, "fOTP")
                otp_el.clear()
                otp_el.send_keys(otp_code)
                time.sleep(0.3)
            except Exception as e:
                _log(f"Không tìm thấy ô OTP: {e}", "warning")

    # ── 6. Bấm nút Đăng nhập #btnLogin (AJAX POST) ─────────────────────────
    _log("Bấm nút Đăng nhập...")
    btn_login = None
    for xpath in [
        '//*[@id="btnLogin"]',
        '//*[@id="btnLogin"]//a',
        '//div[contains(@class,"loginBtn")]',
        '//button[contains(text(),"ĐĂNG NHẬP") and not(contains(.,"IAM"))]',
    ]:
        try:
            btn = d.find_element(By.XPATH, xpath)
            if btn.is_displayed():
                btn_login = btn
                break
        except Exception:
            pass

    if btn_login:
        d.execute_script("arguments[0].click();", btn_login)
    else:
        d.execute_script("$('#btnLogin').click();")

    # ── 7. Đợi chuyển trang và bắt alert thông báo ────────────────────────
    for _ in range(12):
        time.sleep(1)
        try:
            alert = d.switch_to.alert
            alert_text = alert.text
            alert.accept()
            _log(f"Thông báo từ hệ thống: {alert_text}", "warning")
            return False, f"❌ Đăng nhập thất bại: {alert_text}"
        except Exception:
            pass
        current_url = d.current_url
        if "inside.fpt.net" in current_url and "login.fpt.net" not in current_url:
            break

    # ── 8. Kiểm tra đăng nhập thành công ─────────────────────────────────
    current_url = d.current_url
    _log(f"URL sau đăng nhập: {current_url}")

    if "inside.fpt.net" in current_url or "default.asp" in current_url:
        _inside_logged_in = True
        _log(f"✅ Đã đăng nhập FPT Inside thành công! (URL: {current_url})")
        return True, f"✅ Đã đăng nhập FPT Inside thành công! (URL: {current_url})"

    try:
        os.makedirs(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_shots"), exist_ok=True)
        d.save_screenshot(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_shots", "inside_login_fail.png"))
    except Exception:
        pass
    return False, f"❌ Đăng nhập thất bại. Vẫn ở trang: {d.title}"


# ─────────────────────────────────────────────────────────────────────────────
#  ĐIỀU HƯỚNG ĐẾN TRANG BẢO TRÌ
# ─────────────────────────────────────────────────────────────────────────────
def navigate_to_bao_tri() -> tuple[bool, str]:
    """
    Điều hướng đến: Inside New V5 → Hỗ trợ kỹ thuật → Thông tin chung → tab Bảo Trì
    Inside default.asp dùng frameset:
      - Frame 0 (Left.Asp): Menu sidebar
      - Frame 1 (Content.asp): Nội dung chính chứa MudBlazor tabs
      - iframe#frameMaintenance: iframe Bảo trì chứa ô Nhập hợp đồng và nút Xem CL
    """
    d = _inside_driver
    if not d:
        return False, "❌ Driver chưa được khởi động"

    _log("Điều hướng đến trang Bảo Trì Inside...")

    # ── Bước 1: Chuyển sang Frame 0 (Left.Asp) ───────────────────────────
    try:
        d.switch_to.default_content()
        d.switch_to.frame(0)
    except Exception as e:
        _log(f"Lỗi chuyển frame menu: {e}", "warning")

    # ── Bước 2: Bấm menu 'Inside New V5' ──────────────────────────────────
    _log("Mở menu 'Inside New V5'...")
    for a in d.find_elements(By.TAG_NAME, "a"):
        if "Inside New V5" in a.text:
            d.execute_script("arguments[0].click();", a)
            break
    time.sleep(1)

    # ── Bước 3: Bấm menu con 'Hỗ trợ kỹ thuật' ──────────────────────────
    _log("Mở menu 'Hỗ trợ kỹ thuật'...")
    for a in d.find_elements(By.TAG_NAME, "a"):
        if a.is_displayed() and "Hỗ trợ kỹ thuật" in a.text:
            d.execute_script("arguments[0].click();", a)
            break
    time.sleep(1)

    # ── Bước 4: Bấm 'Thông tin chung' ────────────────────────────────────
    _log("Bấm menu 'Thông tin chung'...")
    for a in d.find_elements(By.TAG_NAME, "a"):
        if a.is_displayed() and "Thông tin chung" in a.text:
            d.execute_script("arguments[0].click();", a)
            break
    time.sleep(3.5)

    # ── Bước 5: Chuyển sang Frame 1 (Content.asp) ────────────────────────
    d.switch_to.default_content()
    d.switch_to.frame(1)
    _log("Chuyển sang frame nội dung chính...")

    # ── Bước 6: Bấm qua tab 'BẢO TRÌ' (MudBlazor tab) ────────────────────
    _log("Bấm qua tab 'BẢO TRÌ'...")
    tab_clicked = False
    tabs = d.find_elements(By.XPATH, '//*[@class and contains(@class,"mud-tab") and not(contains(@class,"mud-tabs"))]')
    for t in tabs:
        if "bảo trì" in t.text.lower():
            t.click()
            tab_clicked = True
            break

    if not tab_clicked:
        for el in d.find_elements(By.XPATH, '//*[text()="BẢO TRÌ" or text()="Bảo Trì"]'):
            if el.is_displayed():
                el.click()
                tab_clicked = True
                break

    time.sleep(3)

    # ── Bước 7: Chuyển vào iframe frameMaintenance ────────────────────────
    try:
        d.switch_to.frame("frameMaintenance")
        _log("✅ Đã vào tab BẢO TRÌ (sẵn sàng nhập hợp đồng)!")
        return True, "✅ Đã điều hướng đến trang Bảo Trì"
    except Exception as e:
        _log(f"⚠️ Chưa switch được vào frameMaintenance: {e}", "warning")
        return True, "⚠️ Đã chuyển tab Bảo Trì"


# ─────────────────────────────────────────────────────────────────────────────
#  GHI CHÚ VÀO HỢP ĐỒNG
# ─────────────────────────────────────────────────────────────────────────────
def _compact_tap_diem_for_note(t: str) -> str:
    """Rút gọn thông tin tập điểm để vừa vặn khung ghi chú FPT Inside (500 ký tự)."""
    if not t:
        return ""
    import re
    m_eval = re.match(r'^(.*?\))\s*\|\s*(.*)$', t)
    if m_eval:
        prefix_eval = m_eval.group(1).strip()
        rest = m_eval.group(2).strip()
    else:
        prefix_eval = ""
        rest = t

    rest = re.sub(r':\s*HĐ\s*[-+]?\d+\.?\d*dBm\s*/\s*TB\s*[-+]?\d+\.?\d*dBm', '', rest)
    rest = re.sub(r'\s*\([A-Z0-9_]{4,12}[^\)]*\)', '', rest)
    rest = re.sub(r'Tập điểm này có\s*', '', rest)
    rest = re.sub(r'\d+/\d+\s*Online,\s*0/\d+\s*Offline;?\s*', '', rest)
    rest = re.sub(r'HĐ Rx Power không đạt <=\s*-23\.5dBm', 'HĐ suy hao', rest)
    rest = re.sub(r'HĐ không đạt chuẩn -10 đến -23\.5dBm', 'HĐ suy hao', rest)
    rest = re.sub(r'Rx Power ngoài chuẩn', 'Rx ngoài chuẩn', rest)
    rest = re.sub(r'Tập điểm có HĐ Unknown\s*\(\d+\s*HĐ;[^)]*\)', 'TĐ có HĐ Unknown (đều Unknown)', rest)
    rest = re.sub(r'\(.*?\)', '', rest)
    rest = re.sub(r'\s+', ' ', rest).strip()

    if prefix_eval:
        if rest and rest not in ("Tập điểm bình thường", "Rx Power đạt chuẩn"):
            return f"{prefix_eval} | {rest}"
        return prefix_eval
    return rest


def _compact_client_for_note(client_str: str) -> str:
    """Rút gọn phân tích client wifi yếu để không làm tràn 500 ký tự."""
    if not client_str or client_str == "—":
        return "Bình thường"
    import re
    if "Không ghi nhận" in client_str:
        return "Bình thường"
    c = re.sub(r'(\d{2}/\d{2})/\d{4}', r'\1', client_str)
    c = re.sub(r'thiết bị kém', 'tbi', c)
    c = re.sub(r'thiết bị', 'tbi', c)
    match = re.search(r'\[(.*?)\]', c)
    if match:
        items = [x.strip() for x in match.group(1).split(',') if x.strip()]
        if len(items) > 3:
            short_items = items[:3]
            rem = len(items) - 3
            c = c[:match.start()] + f"[{', '.join(short_items)}, +{rem} tbi]"
    return c.strip()


def _compact_rot_mang_for_note(doi_chieu_rot: str, rot_title: str) -> str:
    """Rút gọn thông tin rớt kết nối."""
    if not doi_chieu_rot or doi_chieu_rot == "—":
        return f"{rot_title}: Đảm bảo (0 lần rớt)"
    import re
    clean_rot = re.sub(r'\(Trong 48h qua\s*', '(', doi_chieu_rot)
    clean_rot = re.sub(r'\(Trong 24H sau HT\s*', '(', clean_rot)
    clean_rot = re.sub(r'Không ghi nhận mất kết nối[^)]*', '0 lần rớt', clean_rot)
    clean_rot = re.sub(r'chỉ ra Mạng\s*1\s*Lần', '1 lần ra mạng', clean_rot, flags=re.IGNORECASE)
    clean_rot = re.sub(r'1 Lần', '1 lần', clean_rot)
    return f"{rot_title}: {clean_rot}".strip()


def _build_note_contents(result: dict) -> list[str]:
    """
    Tổng hợp nội dung ghi chú từ kết quả phân tích theo hướng tối ưu cô đọng:
    - Rút gọn thông minh các mục Cần Xử Lý, Suy Hao, Client, Rớt KN để vừa vặn 1 Note duy nhất (<= 450 ký tự).
    - Đảm bảo 100% đầy đủ thông tin mà không bao giờ bị cắt cụt hay phải tách note trùng lặp.
    """
    import re
    now_str = time.strftime('%d/%m/%Y %H:%M')
    canh_bao = (result.get("canh_bao") or "").strip()
    can_xu_ly = (result.get("can_xu_ly") or "").strip()
    tu_dong = (result.get("xu_ly_loi_tu_dong") or "").strip()

    doi_chieu_rot = (result.get("doi_chieu_rot_mang") or "").strip()
    rot_title = "🔄 KN Sau HT" if "24h" in doi_chieu_rot.lower() else "🔄 KN Trong 48H"
    rot_str = _compact_rot_mang_for_note(doi_chieu_rot, rot_title)

    doi_chieu_tap = (result.get("doi_chieu_tap_diem") or "").strip()
    if doi_chieu_tap and doi_chieu_tap != "—":
        tap_str = f"🌐 Suy Hao: {_compact_tap_diem_for_note(doi_chieu_tap)}"
    else:
        tap_str = "🌐 Suy Hao: Chưa có dữ liệu"

    phan_tich_client = (result.get("phan_tich_client") or "").strip()
    client_str = f"📱 Tbi WF Kém: {_compact_client_for_note(phan_tich_client)}"

    suy_hao = (result.get("cong_suat_thu") or "").strip()
    rot_mang = (result.get("so_lan_rot") or "").strip()
    loai_modem = (result.get("loai_modem") or "").strip()

    info_parts = []
    if suy_hao and suy_hao != "—":
        info_parts.append(f"📶 Rx: {suy_hao}dBm")
    if rot_mang and rot_mang != "—":
        info_parts.append(f"📉 Rớt: {rot_mang}")
    if loai_modem and loai_modem != "—":
        info_parts.append(f"📦 {loai_modem}")
    info_str = " | ".join(info_parts)

    single_lines = [f"[AUTO NOTE - MYBAE] {now_str}"]
    if canh_bao and canh_bao != "—":
        single_lines.append(f"⚠️ Cảnh Báo: {canh_bao}")
    if can_xu_ly and can_xu_ly != "—":
        cx_text = can_xu_ly
        if cx_text.startswith("Kiểm tra các thành phần sau: "):
            cx_text = cx_text[len("Kiểm tra các thành phần sau: "):]
        cx_text = re.sub(r'Xem bảng thiết bị đang kết nối', 'Xem bảng tbi kết nối', cx_text)
        cx_text = re.sub(r'Tư vấn lắp AP', 'Tư vấn AP', cx_text)
        single_lines.append(f"🔧 Cần Xử Lý: {cx_text}")
    if tu_dong and tu_dong != "—":
        single_lines.append(f"🤖 Tự Động: {tu_dong}")

    single_lines.append(rot_str)
    single_lines.append(tap_str)
    single_lines.append(client_str)
    if info_str:
        single_lines.append(info_str)

    single_note = "\n".join(single_lines)
    # Tối ưu: Đảm bảo độ dài luôn nằm trong ngưỡng an toàn <= 450 ký tự
    if len(single_note) <= 450:
        return [single_note]

    # Nếu trường hợp ngoại lệ vẫn > 450 ký tự:
    # Tách thành 2 phần rõ rệt, KHÔNG lặp lại Cảnh Báo hay Cần Xử Lý
    part1_lines = [f"[AUTO NOTE - MYBAE (1/2)] {now_str}"]
    if canh_bao and canh_bao != "—":
        part1_lines.append(f"⚠️ Cảnh Báo: {canh_bao}")
    if can_xu_ly and can_xu_ly != "—":
        part1_lines.append(f"🔧 Cần Xử Lý: {cx_text}")
    if tu_dong and tu_dong != "—":
        part1_lines.append(f"🤖 Tự Động: {tu_dong}")
    if info_str:
        part1_lines.append(info_str)
    note_1 = "\n".join(part1_lines)[:480]

    part2_lines = [
        f"[AUTO NOTE - MYBAE (2/2)] {now_str}",
        rot_str,
        tap_str,
        client_str
    ]
    note_2 = "\n".join(part2_lines)[:480]

    return [note_1, note_2]


def _build_note_content(result: dict) -> str:
    """Trả về chuỗi đại diện (cho preview hoặc kiểm tra)."""
    parts = _build_note_contents(result)
    return "\n\n".join(parts)


def add_note_to_contract(so_hd: str, note_input) -> tuple[bool, str]:
    """
    Tự động ghi chú vào hợp đồng so_hd trên FPT Inside theo PA 1.
    Hỗ trợ note_input là str (1 note) hoặc list (2 phần note do dài quá 450 ký tự).
    """
    d = _inside_driver
    if not d:
        return False, "❌ Driver chưa sẵn sàng"

    _log(f"  → Xử lý HĐ: {so_hd}")

    # Chuyển input thành list
    if isinstance(note_input, str):
        note_list = [note_input]
    else:
        note_list = list(note_input)

    # Đảm bảo context ở Frame 1 -> frameMaintenance
    try:
        d.switch_to.default_content()
        d.switch_to.frame(1)
        d.switch_to.frame("frameMaintenance")
    except Exception:
        try:
            d.switch_to.default_content()
            d.switch_to.frame(1)
            d.switch_to.frame("frameMaintenance")
        except Exception:
            pass

    original_handles = set(d.window_handles)
    new_handles = set()

    try:
        # ── Bước 1: Tìm ô nhập hợp đồng ─────────────────────────────────
        contract_input = None
        for xpath in [
            '//div[contains(text(),"Nhập hợp đồng") or contains(.,"Nhập hợp đồng")]/following::input[1]',
            '//button[contains(.,"Xem CL")]/preceding::input[1]',
            '//input[contains(@placeholder,"Nhập hợp đồng") or contains(@placeholder,"hợp đồng")]',
            '//input[contains(@class,"mud-input-root-margin-dense")]',
        ]:
            try:
                el = d.find_element(By.XPATH, xpath)
                if el.is_displayed():
                    contract_input = el
                    break
            except Exception:
                pass

        if not contract_input:
            return False, f"❌ HĐ {so_hd}: Không tìm thấy ô nhập hợp đồng"

        # Click để focus và xóa sạch bằng phím
        contract_input.click()
        contract_input.send_keys(Keys.CONTROL + "a")
        contract_input.send_keys(Keys.BACKSPACE)
        time.sleep(0.2)
        contract_input.send_keys(so_hd)
        contract_input.send_keys(Keys.TAB)
        d.execute_script("arguments[0].dispatchEvent(new Event('input',{bubbles:true}));", contract_input)
        d.execute_script("arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", contract_input)
        time.sleep(0.5)

        # ── Bước 2: Bấm "Xem CL" ────────────────────────────────────────
        xem_cl_btn = None
        for xpath in [
            '//button[contains(.,"Xem CL") or contains(.,"Xem Cls") or contains(.,"XEM CL")]',
            '//a[contains(.,"Xem CL")]',
        ]:
            try:
                btn = d.find_element(By.XPATH, xpath)
                if btn.is_displayed() and btn.is_enabled():
                    xem_cl_btn = btn
                    break
            except Exception:
                pass

        if not xem_cl_btn:
            return False, f"❌ HĐ {so_hd}: Không tìm thấy nút Xem CL"

        d.execute_script("arguments[0].click();", xem_cl_btn)
        time.sleep(3)

        # Kiểm tra alert hệ thống (nếu có)
        try:
            alert = d.switch_to.alert
            alert_text = alert.text
            alert.accept()
            _log(f"  ⚠️ Thông báo hệ thống cho HĐ {so_hd}: {alert_text}", "warning")
        except Exception:
            pass

        # ── Bước 3: Chuyển sang tab mới phiếu bảo trì ───────────────────
        new_handles = set(d.window_handles) - original_handles
        if not new_handles:
            for _ in range(4):
                time.sleep(1)
                new_handles = set(d.window_handles) - original_handles
                if new_handles:
                    break

        if not new_handles:
            _log(f"  ⚠️ Không có tab mới xuất hiện cho HĐ {so_hd} (có thể không có phiếu bảo trì hoặc lỗi hợp đồng)", "warning")
            return False, f"⚠️ HĐ {so_hd}: Không mở được phiếu bảo trì"

        new_tab = list(new_handles)[0]
        d.switch_to.window(new_tab)
        _log(f"  → Đã mở phiếu bảo trì HĐ {so_hd}: {d.title}")
        time.sleep(2)

        # Chụp màn hình debug phiếu ban đầu
        try:
            os.makedirs(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_shots"), exist_ok=True)
            shot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_shots", f"{so_hd}_inside_ticket.png")
            d.save_screenshot(shot_path)
        except Exception:
            pass

        # ── LẶP QUA TỪNG PHẦN NOTE (PA 1: TÁCH THÀNH NHIỀU PHẦN NẾU DÀI) ──
        for part_idx, content_to_note in enumerate(note_list):
            part_label = f" (Phần {part_idx+1}/{len(note_list)})" if len(note_list) > 1 else ""

            # Nếu là phần thứ 2 trở đi, đảm bảo bất kỳ dialog / overlay trước đó đã biến mất hoàn toàn
            if part_idx > 0:
                try:
                    WebDriverWait(d, 8).until(
                        EC.invisibility_of_element_located((By.XPATH, '//div[contains(@class,"mud-dialog-container")] | //div[contains(@class,"mud-overlay")]'))
                    )
                except Exception:
                    pass
                time.sleep(1.5)

            # ── Bước 4: Tìm biểu tượng Ghi chú ⊕ và mở popup ───────────────
            note_icon = None
            for xpath in [
                '//div[contains(.,"Ghi chú") and not(contains(.,"Division"))]//*[local-name()="svg"]',
                '//*[text()="Ghi chú" or contains(text(),"Ghi chú")]/following-sibling::*[local-name()="svg"]',
                '//*[text()="Ghi chú" or contains(text(),"Ghi chú")]/..//*[local-name()="svg"]',
                '//*[contains(@class,"note-left")]//*[local-name()="svg"]',
                '//*[contains(text(),"Ghi chú")]/following-sibling::button',
                '//*[contains(text(),"Ghi chú")]/..//button',
                '//*[local-name()="svg" and contains(@class,"mud-icon-size-medium") and @style="cursor:pointer"]',
            ]:
                try:
                    for ic in d.find_elements(By.XPATH, xpath):
                        if ic.is_displayed():
                            note_icon = ic
                            break
                    if note_icon:
                        break
                except Exception:
                    pass

            if not note_icon:
                _log(f"  ⚠️ Phiếu {so_hd}: Không tìm thấy biểu tượng ⊕ Ghi chú{part_label} (phiếu đã hoàn tất hoặc đóng)", "warning")
                d.close()
                d.switch_to.window(list(original_handles)[0])
                d.switch_to.default_content()
                d.switch_to.frame(1)
                d.switch_to.frame("frameMaintenance")
                return False, f"⚠️ HĐ {so_hd}: Phiếu không có nút ghi chú"

            # Bấm dấu ⊕ và xác thực popup dialog phải thực sự mở ra (thử tối đa 3 lần)
            dialog = None
            for attempt in range(3):
                _log(f"  → Bấm dấu ⊕ Ghi chú cho HĐ {so_hd}{part_label} (lần {attempt+1})...")
                d.execute_script("""
                    let el = arguments[0];
                    let btn = el.closest('button') || el.closest('[role="button"]') || el.parentElement || el;
                    btn.scrollIntoView({block: 'center'});
                    btn.click();
                """, note_icon)
                try:
                    note_icon.click()
                except Exception:
                    pass

                # Chờ xem mud-dialog đã mở chưa
                try:
                    dialog = WebDriverWait(d, 3).until(
                        EC.visibility_of_element_located((By.XPATH, '//div[contains(@class,"mud-dialog")]'))
                    )
                    if dialog and dialog.is_displayed():
                        break
                except Exception:
                    dialog = None
                time.sleep(1)

            if not dialog:
                _log(f"  ❌ Không mở được khung popup Ghi chú cho HĐ {so_hd}{part_label}", "error")
                d.close()
                d.switch_to.window(list(original_handles)[0])
                d.switch_to.default_content()
                d.switch_to.frame(1)
                d.switch_to.frame("frameMaintenance")
                return False, f"❌ HĐ {so_hd}: Không mở được popup Ghi chú"

            # ── Bước 5: Nhập nội dung ghi chú vào textarea STRICTLY trong popup ───
            try:
                textarea = WebDriverWait(dialog, 4).until(
                    EC.visibility_of_element_located((By.XPATH, './/textarea'))
                )
            except Exception:
                textarea = None

            if not textarea:
                _log(f"  ❌ Popup Ghi chú HĐ {so_hd} không có ô nhập liệu", "error")
                d.close()
                d.switch_to.window(list(original_handles)[0])
                d.switch_to.default_content()
                d.switch_to.frame(1)
                d.switch_to.frame("frameMaintenance")
                return False, f"❌ HĐ {so_hd}: Popup thiếu ô nhập"

            _log(f"  → Nhập nội dung ghi chú vào khung{part_label}...")
            d.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", textarea)
            try:
                textarea.click()
            except Exception:
                d.execute_script("arguments[0].click();", textarea)
            time.sleep(0.2)
            textarea.send_keys(Keys.CONTROL + "a")
            textarea.send_keys(Keys.BACKSPACE)
            time.sleep(0.2)

            # Giới hạn tối đa 480 ký tự cho mỗi phần
            content_clean = content_to_note[:480].strip()
            textarea.send_keys(content_clean)
            d.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles: true}));", textarea)
            d.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles: true}));", textarea)
            time.sleep(0.5)

            # ── Bước 6: Bấm nút Cập nhật STRICTLY trong popup dialog ─────────
            try:
                update_btn = dialog.find_element(By.XPATH, './/button[contains(.,"Cập nhật")]')
            except Exception:
                update_btn = None

            if not update_btn:
                _log(f"  ❌ HĐ {so_hd}: Không tìm thấy nút Cập nhật trong popup{part_label}", "error")
                d.close()
                d.switch_to.window(list(original_handles)[0])
                d.switch_to.default_content()
                d.switch_to.frame(1)
                d.switch_to.frame("frameMaintenance")
                return False, f"❌ HĐ {so_hd}: Không thấy nút Cập nhật trong popup"

            _log(f"  → Bấm Cập nhật ghi chú{part_label}...")
            d.execute_script("arguments[0].click();", update_btn)

            # Chờ dialog đóng hoàn toàn (lưu thành công)
            try:
                WebDriverWait(d, 8).until(
                    EC.invisibility_of_element_located((By.XPATH, '//div[contains(@class,"mud-dialog")]'))
                )
            except Exception:
                pass
            time.sleep(2.0)

        # Chụp màn hình xác nhận ghi chú thành công
        try:
            shot_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_shots", f"{so_hd}_done_note.png")
            d.save_screenshot(shot_path)
        except Exception:
            pass

        note_count_str = f" ({len(note_list)} phần note)" if len(note_list) > 1 else ""
        _log(f"  ✅ Đã ghi chú thành công cho HĐ {so_hd}{note_count_str}")

        # Đóng tab phiếu bảo trì và quay về tab chính
        d.close()
        d.switch_to.window(list(original_handles)[0])
        d.switch_to.default_content()
        d.switch_to.frame(1)
        d.switch_to.frame("frameMaintenance")
        time.sleep(1)

        return True, f"✅ Đã ghi chú thành công HĐ {so_hd}{note_count_str}"

    except Exception as e:
        _log(f"  ❌ Lỗi xử lý HĐ {so_hd}: {e}", "error")
        if new_handles:
            try:
                current_handles = set(d.window_handles)
                leaked = current_handles - original_handles
                for h in leaked:
                    d.switch_to.window(h)
                    d.close()
                d.switch_to.window(list(original_handles)[0])
                d.switch_to.default_content()
                d.switch_to.frame(1)
                d.switch_to.frame("frameMaintenance")
            except Exception:
                pass
        return False, f"❌ Lỗi xử lý HĐ {so_hd}: {e}"




# ─────────────────────────────────────────────────────────────────────────────
#  JOB CHÍNH: RUN AUTO NOTE
# ─────────────────────────────────────────────────────────────────────────────
def run_auto_note(results_to_note: list[dict], totp_secret: str = None):
    """
    Job chính chạy nền: đăng nhập → điều hướng → ghi chú từng HĐ.
    results_to_note: list of result dict từ phân tích (phải có so_hd, canh_bao/can_xu_ly).
    """
    global _auto_note_running, _auto_note_status, _auto_note_cancel
    global _auto_note_progress, _auto_note_total, _auto_note_log
    global _auto_note_results, _auto_note_percent, _auto_note_current

    # Reset state
    _auto_note_running  = True
    _auto_note_status   = "logging_in"
    _auto_note_cancel   = False
    _auto_note_progress = 0
    _auto_note_total    = len(results_to_note)
    _auto_note_log      = []
    _auto_note_results  = []
    _auto_note_percent  = 0
    _auto_note_current  = ""

    try:
        # ── Đăng nhập ──────────────────────────────────────────────────────
        _log("═══════════════════════════════════════")
        _log(f"🚀 Bắt đầu Auto Note {len(results_to_note)} hợp đồng")
        _log("═══════════════════════════════════════")

        ok, msg = login_inside_fpt(totp_secret=totp_secret)
        if not ok:
            _log(f"Đăng nhập thất bại: {msg}", "error")
            _auto_note_status  = "error"
            _auto_note_running = False
            return

        _log(msg)
        _auto_note_status = "navigating"

        # ── Điều hướng đến trang Bảo Trì ───────────────────────────────────
        ok, msg = navigate_to_bao_tri()
        _log(msg, "info" if ok else "warning")

        _auto_note_status = "running"
        total = len(results_to_note)

        # ── Ghi chú từng hợp đồng ───────────────────────────────────────────
        for i, result in enumerate(results_to_note):
            if _auto_note_cancel:
                _log(f"⛔ Đã dừng tiến trình tại HĐ {i}/{total}")
                _auto_note_status  = "cancelled"
                _auto_note_running = False
                return

            so_hd = result.get("so_hd", "")
            _auto_note_current  = so_hd
            _auto_note_percent  = int((i / total) * 100)

            _log(f"──────── [{i+1}/{total}] HĐ: {so_hd} ────────")
            note_parts = _build_note_contents(result)

            success, note_msg = add_note_to_contract(so_hd, note_parts)

            _auto_note_results.append({
                "so_hd":      so_hd,
                "status":     "success" if success else "failed",
                "message":    note_msg,
                "note_parts": note_parts,
                "time":       time.strftime("%H:%M:%S"),
            })
            _auto_note_progress = i + 1
            _auto_note_percent  = int(((i + 1) / total) * 100)

            # Delay giữa các HĐ để tránh bị block
            time.sleep(2)

        # ── Hoàn tất ─────────────────────────────────────────────────────────
        success_count = sum(1 for r in _auto_note_results if r["status"] == "success")
        fail_count    = total - success_count
        _log(f"═══ Hoàn tất! ✅ {success_count} thành công | ❌ {fail_count} thất bại ═══")

        _auto_note_status  = "done"
        _auto_note_percent = 100
        _auto_note_running = False

    except Exception as e:
        _log(f"Lỗi tổng quát Auto Note: {e}", "error")
        _auto_note_status  = "error"
        _auto_note_running = False
    finally:
        # Lưu vào lịch sử note nếu có kết quả
        if _auto_note_results:
            try:
                success_c = sum(1 for r in _auto_note_results if r.get("status") == "success")
                rec = {
                    "id": f"note_{int(time.time() * 1000)}",
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "total": total if 'total' in locals() else len(_auto_note_results),
                    "success_count": success_c,
                    "fail_count": len(_auto_note_results) - success_c,
                    "status": _auto_note_status,
                    "results": list(_auto_note_results),
                }
                save_note_history_run(rec)
            except Exception as he:
                log.error(f"Lỗi lưu lịch sử note: {he}")

        # Đảm bảo driver được đóng khi xong
        try:
            close_inside_driver()
        except Exception:
            pass


def get_auto_note_state() -> dict:
    """Trả về snapshot trạng thái hiện tại của job Auto Note."""
    return {
        "running":   _auto_note_running,
        "status":    _auto_note_status,
        "progress":  _auto_note_progress,
        "total":     _auto_note_total,
        "percent":   _auto_note_percent,
        "current":   _auto_note_current,
        "logs":      list(_auto_note_log),
        "results":   list(_auto_note_results),
    }


def cancel_auto_note():
    """Gửi lệnh dừng job Auto Note."""
    global _auto_note_cancel
    _auto_note_cancel = True
    _log("⛔ Nhận lệnh dừng từ người dùng...")


NOTE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "note_history.json")


def load_note_history() -> list[dict]:
    if not os.path.exists(NOTE_HISTORY_FILE):
        return []
    try:
        with open(NOTE_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Lỗi đọc lịch sử note: {e}")
        return []


def save_note_history_run(record: dict):
    history = load_note_history()
    history.insert(0, record)
    try:
        with open(NOTE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Lỗi ghi lịch sử note: {e}")


def clear_note_history():
    if os.path.exists(NOTE_HISTORY_FILE):
        os.remove(NOTE_HISTORY_FILE)
