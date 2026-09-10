"""
analyzer.py - Phân tích từng hợp đồng trên management.mypt.vn
Chiến lược: dùng body.text (line-by-line parsing) thay vì XPath/CSS phức tạp.
DOM dump cho thấy:
  - Tab counts: "Cảnh báo\n2" (số riêng dòng, không có ngoặc đơn)
  - Không có <td> - MUI dùng <div>
  - body.text đọc được đầy đủ và chính xác
"""
import time
import logging
import os

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import MANAGEMENT_URL
from modules.auth import get_driver

log = logging.getLogger("analyzer")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug_shots")


# ─────────────────────────────────────────────────────────────
#  BODY TEXT PARSER
# ─────────────────────────────────────────────────────────────

def _get_body_lines(d) -> list[str]:
    """Lấy toàn bộ body text và tách thành danh sách dòng (lọc rỗng)."""
    try:
        raw = d.find_element(By.TAG_NAME, "body").text
        return [ln.strip() for ln in raw.split("\n") if ln.strip()]
    except Exception:
        return []


def _find_value_after_label(lines: list[str], label: str) -> str:
    """
    Tìm label trong danh sách dòng → trả về dòng KẾ TIẾP không rỗng.
    Bỏ qua nếu dòng kế là header bảng (Thông số / Giá trị).
    """
    skip = {"Thông số", "Giá trị", "Chi tiết", "Kết quả chẩn đoán",
            "Thông số hệ thống", "Thông số modem", "Thông số wifi"}
    for i, ln in enumerate(lines):
        if ln == label:
            # Lấy dòng kế tiếp không rỗng
            for j in range(i + 1, min(i + 4, len(lines))):
                nxt = lines[j].strip()
                if nxt and nxt not in skip and nxt != label:
                    return nxt
    return ""


def _get_tab_count(lines: list[str], keyword: str) -> int:
    """
    Tab counts: DOM structure là "Cảnh báo\n2" (số riêng dòng, không ngoặc).
    Tìm dòng chứa keyword → dòng kế là số đếm.
    """
    for i, ln in enumerate(lines):
        if keyword in ln and len(ln) < 60:
            # Dòng kế tiếp có phải là số không?
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt.isdigit():
                    return int(nxt)
            # Hoặc số nằm trong cùng dòng (vd: "Cảnh báo 2")
            import re
            m = re.search(r'\b(\d+)\b', ln[len(keyword):])
            if m:
                return int(m.group(1))
    return 0


# ─────────────────────────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────────────────────────

def _text(el) -> str:
    try:
        return (el.text or "").strip()
    except Exception:
        return ""


def _js_click(d, el):
    d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.2)
    d.execute_script("arguments[0].click();", el)


def _shot(d, name: str, step: str):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        d.save_screenshot(os.path.join(DEBUG_DIR, f"{name}_{step}.png"))
    except Exception:
        pass


def _click_mui_tab(d, keyword: str) -> bool:
    """Click MuiTab-root button chứa keyword (từ DOM dump: class='MuiButtonBase-root MuiTab-root')."""
    xpaths = [
        f'//button[contains(@class,"MuiTab-root") and contains(normalize-space(.),"{keyword}")]',
        f'//button[@role="tab" and contains(normalize-space(.),"{keyword}")]',
        f'//*[@role="tab" and contains(normalize-space(.),"{keyword}")]',
        f'//span[normalize-space(text())="{keyword}"]/ancestor::button',
        f'//span[normalize-space(text())="{keyword}"]',
    ]
    for xpath in xpaths:
        try:
            for el in d.find_elements(By.XPATH, xpath):
                if el.is_displayed():
                    _js_click(d, el)
                    time.sleep(2)
                    log.debug(f"  Clicked tab: {keyword}")
                    return True
        except Exception:
            pass
    log.debug(f"  Không click được tab: {keyword}")
    return False


def _find_visible(d, xpaths: list):
    for xpath in xpaths:
        try:
            for el in d.find_elements(By.XPATH, xpath):
                if el.is_displayed():
                    return el
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────
#  FLOW STEPS
# ─────────────────────────────────────────────────────────────

def _go_check_contract(d):
    if "/check-contract" not in d.current_url:
        el = _find_visible(d, [
            '//span[normalize-space(text())="Kiểm tra hợp đồng"]',
            '//*[normalize-space(text())="Kiểm tra hợp đồng"]',
        ])
        if el:
            _js_click(d, el)
            time.sleep(3)
        else:
            d.get(f"{MANAGEMENT_URL}/check-contract")
            time.sleep(3)
    log.info(f"  URL: {d.current_url}")


def _enter_and_analyze(d, contract_number: str):
    inp = None
    for sel in ['#contract', 'input[type="text"]', 'input[type="search"]']:
        try:
            for el in d.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    inp = el
                    break
            if inp:
                break
        except Exception:
            pass

    if not inp:
        raise Exception("Không tìm thấy ô nhập số hợp đồng")

    inp.click()
    time.sleep(0.2)
    inp.send_keys(Keys.CONTROL, 'a')
    inp.send_keys(Keys.DELETE)
    inp.clear()
    time.sleep(0.1)
    inp.send_keys(str(contract_number))
    d.execute_script("arguments[0].dispatchEvent(new Event('input',{bubbles:true}));", inp)
    time.sleep(0.3)
    log.info(f"  Nhập: '{inp.get_attribute('value')}'")

    btn = _find_visible(d, [
        '//button[normalize-space(text())="Phân tích"]',
        '//button[normalize-space(text())="Phân Tích"]',
        '//button[contains(normalize-space(.),"Phân t")]',
        '//button[@type="submit"]',
    ])
    if not btn:
        raise Exception("Không tìm thấy nút Phân tích")
    _js_click(d, btn)
    log.info(f"  Bấm: '{_text(btn)}'")


def _wait_results(d):
    """Chờ 'Tiến hành chẩn đoán...' biến mất, tối đa 45s."""
    log.info("  Chờ chẩn đoán...")
    deadline = time.time() + 45
    while time.time() < deadline:
        lines = _get_body_lines(d)
        body_str = "\n".join(lines)
        still_running = any(kw in body_str for kw in
                            ["Tiến hành chẩn đoán", "Tien hanh chan doan", "đang phân tích"])
        has_result = "Kết quả chẩn đoán" in body_str or "Ket qua chan doan" in body_str
        if not still_running and has_result:
            log.info("  ✓ Xong!")
            time.sleep(1.5)
            return True
        time.sleep(2)
    log.warning("  ⚠ Timeout 45s")
    return False


def _read_diag_content(d, active_tab: str) -> str:
    """
    Đọc tên các lỗi trong tab chẩn đoán được chỉ định.
    active_tab: 'Cảnh báo' | 'Xử lý lỗi tự động' | 'Cần xử lý'
    Tìm đúng tab rồi đọc items bên dưới, bỏ qua badge chips cùng tên.
    """
    lines = _get_body_lines(d)

    tab_keywords = {"Xử lý lỗi tự động", "Cần xử lý", "Cảnh báo"}
    noise = {
        "Thông số hệ thống", "Thông số modem", "Thông số wifi",
        "Thiết bị đang kết nối", "Lịch sử thiết bị kết nối kém",
        "Các lần kết nối", "Lịch điều khiển", "Thông số Box",
        "Lưu lượng sử dụng", "Chi tiết", "Thông số", "Giá trị",
        "Kết quả chẩn đoán", "Mô hình mạng", "Chất lượng Wi-Fi",
        "Thoát hợp đồng", "Lịch sử quét", "Xem chi tiết",
        # Badge chips cùng tên tab (không phải nội dung lỗi)
        "Cảnh báo", "Xử lý lỗi tự động", "Cần xử lý",
    }
    hard_stops = {"Chi tiết", "Thông số hệ thống", "Mô hình mạng",
                  "Chất lượng Wi-Fi", "Lưu lượng sử dụng"}

    items = []
    found_tab = False   # Đã tìm thấy tab active_tab chưa
    in_content = False  # Đang trong phần nội dung chưa

    for i, ln in enumerate(lines):
        if not found_tab:
            # Chỉ bắt đầu đọc khi tìm đúng active_tab
            if ln == active_tab:
                found_tab = True
                in_content = True
                # Dòng kế là count (digit) → sẽ bị lọc bởi len/isdigit
        else:
            # Đang trong nội dung của active_tab
            if ln in hard_stops:
                break  # Gặp section mới → dừng
            # Gặp tab header thật khác (tiếp theo là count digit) → dừng
            if ln in tab_keywords and ln != active_tab:
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt.isdigit():
                    break
            # Thu thập item lỗi
            if (len(ln) > 8
                    and not ln.isdigit()
                    and ln not in noise
                    and not ln.startswith("*")
                    and not ln.startswith("Hướng xử lý")
                    and not any(ln.startswith(f"{n}.") for n in range(1, 10))):
                if ln not in items:
                    items.append(ln)

    return " | ".join(items[:5])



def _exit_contract(d):
    el = _find_visible(d, [
        '//button[contains(normalize-space(.),"Thoát hợp đồng")]',
        '//button[contains(normalize-space(.),"Thoat hop dong")]',
        '//*[contains(normalize-space(.),"Thoát hợp đồng")]',
    ])
    if el:
        _js_click(d, el)
        log.info(f"  ✓ Thoát: '{_text(el)}'")
        time.sleep(3)
        return True
    log.warning("  ⚠ Không thấy nút Thoát → về check-contract")
    d.get(f"{MANAGEMENT_URL}/check-contract")
    time.sleep(3)
    return False


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def analyze_contract(contract_number: str) -> dict:
    d = get_driver(headless=False)

    result = {
        "so_hd":             contract_number,
        "xu_ly_loi_tu_dong": "",
        "can_xu_ly":         "",
        "canh_bao":          "",
        "cong_suat_thu":     "",
        "so_lan_rot":        "",
        "loai_modem":        "",
        "phien_ban_pm":      "",
        "dns_wan":           "",
        "status":            "ok",
        "error":             "",
    }

    try:
        log.info(f"\n{'='*55}\n  HĐ: {contract_number}\n{'='*55}")

        # 1. Vào trang
        _go_check_contract(d)

        # 2-3. Nhập + Phân tích
        _enter_and_analyze(d, contract_number)

        # 4. Chờ kết quả
        _wait_results(d)
        _shot(d, contract_number, "1_done")

        # 5. Đọc counts từ body text
        lines = _get_body_lines(d)
        log.debug(f"  Body lines [{len(lines)}]: {lines[:20]}")

        count_auto = _get_tab_count(lines, "Xử lý lỗi tự động")
        count_need = _get_tab_count(lines, "Cần xử lý")
        count_warn = _get_tab_count(lines, "Cảnh báo")
        log.info(f"  Counts: Auto={count_auto}  Need={count_need}  Warn={count_warn}")

        # 6. Đọc nội dung từng tab chẩn đoán có count > 0
        # Mặc định tab "Cảnh báo" đang active (từ screenshot), đọc trước
        if count_warn > 0:
            _click_mui_tab(d, "Cảnh báo")
            result["canh_bao"] = _read_diag_content(d, "Cảnh báo")
            log.info(f"  canh_bao: {result['canh_bao'][:100]}")

        if count_auto > 0:
            _click_mui_tab(d, "Xử lý lỗi tự động")
            result["xu_ly_loi_tu_dong"] = _read_diag_content(d, "Xử lý lỗi tự động")
            log.info(f"  xu_ly: {result['xu_ly_loi_tu_dong'][:100]}")

        if count_need > 0:
            _click_mui_tab(d, "Cần xử lý")
            result["can_xu_ly"] = _read_diag_content(d, "Cần xử lý")
            log.info(f"  can_xu_ly: {result['can_xu_ly'][:100]}")

        # 7. Tab "Thông số hệ thống"
        log.info(f"[{contract_number}] Tab Thông số hệ thống...")
        _click_mui_tab(d, "Thông số hệ thống")
        _shot(d, contract_number, "2_sys")
        lines_sys = _get_body_lines(d)

        result["cong_suat_thu"] = (
            _find_value_after_label(lines_sys, "Công suất thu") or
            _find_value_after_label(lines_sys, "Cong suat thu")
        )
        result["so_lan_rot"] = (
            _find_value_after_label(lines_sys, "Số lần rớt kết nối") or
            _find_value_after_label(lines_sys, "Số lần rớt")
        )
        log.info(f"  cong_suat_thu='{result['cong_suat_thu']}'  so_lan_rot='{result['so_lan_rot']}'")

        # 8. Tab "Thông số modem"
        log.info(f"[{contract_number}] Tab Thông số modem...")
        _click_mui_tab(d, "Thông số modem")
        _shot(d, contract_number, "3_modem")
        lines_modem = _get_body_lines(d)

        result["loai_modem"]   = _find_value_after_label(lines_modem, "Loại modem")
        result["phien_ban_pm"] = _find_value_after_label(lines_modem, "Phiên bản phần mềm")
        result["dns_wan"]      = _find_value_after_label(lines_modem, "DNS WAN")
        log.info(f"  loai_modem='{result['loai_modem']}'  dns_wan='{result['dns_wan']}'")

        log.info(f"[{contract_number}] ✅ Xong")

    except Exception as e:
        log.error(f"[{contract_number}] ❌ {e}", exc_info=True)
        _shot(d, contract_number, "ERR")
        result["status"] = "error"
        result["error"]  = str(e)

    finally:
        log.info(f"[{contract_number}] Thoát HĐ...")
        _exit_contract(d)

    return result
