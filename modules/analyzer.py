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
                            ["Tiến hành chẩn đoán", "Tien hanh chan doan", "đang phân tích", "Đang tiến hành reboot"])
        has_result = "Kết quả chẩn đoán" in body_str or "Ket qua chan doan" in body_str
        if not still_running and has_result:
            log.info("  ✓ Xong!")
            time.sleep(1.5)
            return True
        time.sleep(2)
    log.warning("  ⚠ Timeout 45s")
    return False


def _check_and_handle_reboot_popup(d, timeout_check: int = 8) -> bool:
    """
    Kiểm tra xem Popup 'Khởi động lại modem' có xuất hiện hay không.
    Nội dung popup: "Nhấn Xác nhận để thực hiện khởi động lại thiết bị. Bỏ qua để sang bước tiếp theo"
    Nếu có -> Bấm nút 'XÁC NHẬN', sau đó chờ 3 phút (180 giây) để modem reboot hoàn tất.
    """
    log.info("  Kiểm tra Popup Khởi động lại modem...")
    deadline = time.time() + timeout_check
    while time.time() < deadline:
        try:
            body_lines = _get_body_lines(d)
            body_str = "\n".join(body_lines)
            
            is_reboot_popup = any(kw in body_str for kw in [
                "Khởi động lại modem",
                "thực hiện khởi động lại thiết bị",
                "Bỏ qua để sang bước tiếp theo"
            ])

            if is_reboot_popup:
                confirm_btns = d.find_elements(By.XPATH,
                    '//button[contains(normalize-space(.),"XÁC NHẬN") or contains(normalize-space(.),"Xác nhận") or contains(normalize-space(.),"Xác Nhận")]'
                    ' | //div[contains(@class,"MuiDialog")]//button[contains(.,"XÁC NHẬN") or contains(.,"Xác nhận")]'
                )
                for btn in confirm_btns:
                    if btn.is_displayed() and btn.is_enabled():
                        log.info("  🔄 Phát hiện Popup 'Khởi động lại modem'! Bấm nút XÁC NHẬN...")
                        _js_click(d, btn)
                        time.sleep(3)
                        log.info("  ⏳ Đang chờ 3 phút (180s) để modem khởi động lại hoàn tất...")
                        time.sleep(180)
                        return True
        except Exception as ex:
            log.debug(f"  Lỗi khi kiểm tra popup reboot: {ex}")
        time.sleep(1.5)
    return False


def _is_instruction_line(ln: str) -> bool:
    """Kiểm tra xem dòng text có phải là Hướng xử lý / Thông báo KH / Thao tác kỹ thuật hay không."""
    if not ln:
        return False
    prefixes = (
        "Hướng xử lý", "Thông báo KH", "Kiểm tra", "-", "*", "•",
        "1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9."
    )
    if any(ln.startswith(p) for p in prefixes):
        return True
    action_verbs = (
        "Kiểm tra ", "Xem ", "Di dời ", "Tư vấn ", "Cài đặt ",
        "Khởi động ", "Thay thế ", "Đổi ", "Rút ", "Cắm ", "Thay ", "Báo "
    )
    if any(ln.startswith(v) for v in action_verbs):
        return True
    return False


def _read_diag_details(d, active_tab: str) -> tuple[str, str]:
    """
    Đọc tab chẩn đoán (Cảnh báo / Cần xử lý / Xử lý lỗi tự động), phân tách chính xác:
    1. Tên cảnh báo / Tên lỗi (titles) -> Đưa vào cột tương ứng (Cảnh báo / Xử lý tự động)
    2. Chi tiết Hướng xử lý / Thông báo KH -> Đưa vào cột 'Cần xử lý'
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
        "Cảnh báo", "Xử lý lỗi tự động", "Cần xử lý",
        "XÁC NHẬN", "Bỏ qua", "Quay lại"
    }
    hard_stops = {"Chi tiết", "Thông số hệ thống", "Mô hình mạng",
                  "Chất lượng Wi-Fi", "Lưu lượng sử dụng"}

    titles = []
    instruction_groups = []
    current_instructions = []

    found_tab = False
    is_collecting_instructions = False

    for i, ln in enumerate(lines):
        if not found_tab:
            if ln == active_tab:
                found_tab = True
        else:
            if ln in hard_stops:
                break
            if ln in tab_keywords and ln != active_tab:
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt.isdigit():
                    break

            if ln in noise or ln.isdigit():
                continue

            if _is_instruction_line(ln) or is_collecting_instructions:
                if _is_instruction_line(ln):
                    is_collecting_instructions = True
                    if ln.startswith("Hướng xử lý"):
                        continue
                    current_instructions.append(ln)
                else:
                    if len(ln) > 8 and not _is_instruction_line(ln) and not ln.startswith("-") and not ln.startswith("*"):
                        is_collecting_instructions = False
                        if current_instructions:
                            instruction_groups.append(" ".join(current_instructions))
                            current_instructions = []
                        if ln not in titles:
                            titles.append(ln)
                    else:
                        current_instructions.append(ln)
            else:
                if current_instructions:
                    instruction_groups.append(" ".join(current_instructions))
                    current_instructions = []
                if ln not in titles:
                    titles.append(ln)

    if current_instructions:
        instruction_groups.append(" ".join(current_instructions))

    titles_str = " | ".join(titles)
    huong_xu_ly_str = " | ".join(instruction_groups)
    return titles_str, huong_xu_ly_str



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

def analyze_contract(contract_number: str, tg_hoan_tat: str = "") -> dict:
    d = get_driver(headless=False)

    result = {
        "so_hd":             contract_number,
        "tg_hoan_tat":        tg_hoan_tat,
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

        # 3.5 Kiểm tra xem có Popup 'Khởi động lại modem' hay không
        did_reboot = _check_and_handle_reboot_popup(d, timeout_check=8)
        if did_reboot:
            log.info(f"[{contract_number}] 🔄 Modem đã được khởi động lại xong. Chạy phân tích lại hệ thống...")
            btn_re = _find_visible(d, [
                '//button[normalize-space(text())="Phân tích"]',
                '//button[normalize-space(text())="Phân Tích"]',
                '//button[contains(normalize-space(.),"Phân t")]',
                '//button[@type="submit"]',
            ])
            if btn_re:
                _js_click(d, btn_re)
                time.sleep(2)

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
            warn_titles, warn_hxl = _read_diag_details(d, "Cảnh báo")
            result["canh_bao"] = warn_titles
            if warn_hxl:
                if result["can_xu_ly"]:
                    result["can_xu_ly"] = f"{result['can_xu_ly']} | {warn_hxl}"
                else:
                    result["can_xu_ly"] = warn_hxl
            log.info(f"  canh_bao: {result['canh_bao'][:100]}")
            log.info(f"  hxl_tu_canh_bao: {warn_hxl[:100]}")

        # Cột Xử lý tự động: Ghi nhận 'Reboot modem' nếu có bấm xác nhận reboot
        auto_items = []
        if did_reboot:
            auto_items.append("Reboot modem")

        if count_auto > 0:
            _click_mui_tab(d, "Xử lý lỗi tự động")
            auto_titles, _ = _read_diag_details(d, "Xử lý lỗi tự động")
            if auto_titles and auto_titles not in auto_items:
                auto_items.append(auto_titles)

        result["xu_ly_loi_tu_dong"] = " | ".join(auto_items)
        log.info(f"  xu_ly: {result['xu_ly_loi_tu_dong'][:100]}")

        if count_need > 0:
            _click_mui_tab(d, "Cần xử lý")
            need_titles, need_hxl = _read_diag_details(d, "Cần xử lý")
            need_combined = []
            if need_titles:
                need_combined.append(need_titles)
            if need_hxl:
                need_combined.append(need_hxl)
            
            need_str = " | ".join(need_combined)
            if result["can_xu_ly"]:
                result["can_xu_ly"] = f"{need_str} | {result['can_xu_ly']}"
            else:
                result["can_xu_ly"] = need_str
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

        # 9. ĐỐI CHIẾU CHUYÊN SÂU (Nếu đạt điều kiện)
        # Điều kiện 1: Số lần rớt KN >= 2 -> Đối chiếu tab 'Các lần kết nối' & 'Nguyên nhân rớt kết nối OLT'
        try:
            rot_val = 0
            if result["so_lan_rot"]:
                import re
                m_rot = re.search(r'\d+', str(result["so_lan_rot"]))
                if m_rot:
                    rot_val = int(m_rot.group(0))
            if rot_val >= 2:
                log.info(f"[{contract_number}] 🔍 Thực hiện đối chiếu Rớt Kết Nối (Rớt = {rot_val} >= 2)...")
                tg_hoan_tat_val = result.get("tg_hoan_tat", "")
                result["doi_chieu_rot_mang"] = _cross_check_disconnections(d, tg_hoan_tat_val)
                log.info(f"  doi_chieu_rot_mang: {result['doi_chieu_rot_mang']}")
        except Exception as ex_rot:
            log.warning(f"Lỗi khi đối chiếu rớt kết nối: {ex_rot}")
            result["doi_chieu_rot_mang"] = "Lỗi khi đọc đối chiếu rớt KN"

        # Đối chiếu sub-tab 'Hợp đồng cùng tập điểm' (Luôn thực hiện để hiển thị thông tin tập điểm đầy đủ)
        try:
            log.info(f"[{contract_number}] 🔍 Thực hiện đối chiếu Tập Điểm...")
            result["doi_chieu_tap_diem"] = _cross_check_tap_diem(d)
            log.info(f"  doi_chieu_tap_diem: {result['doi_chieu_tap_diem']}")
        except Exception as ex_tap:
            log.warning(f"Lỗi khi đối chiếu tập điểm: {ex_tap}")
            result["doi_chieu_tap_diem"] = "Lỗi khi đọc đối chiếu tập điểm"

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


def _set_mui_max_pagination(d):
    """Thử bấm dropdown phân trang MUI để chọn 50 hoặc 100 dòng per page."""
    try:
        selects = d.find_elements(By.XPATH,
            '//div[contains(@class,"MuiTablePagination-select")]'
            ' | //div[contains(@class,"MuiSelect-select") and contains(@aria-haspopup,"listbox")]'
        )
        for sel in selects:
            if sel.is_displayed():
                _js_click(d, sel)
                time.sleep(0.5)
                options = d.find_elements(By.XPATH, '//li[@data-value="100" or @data-value="50" or contains(text(),"100") or contains(text(),"50")]')
                for opt in options:
                    if opt.is_displayed():
                        _js_click(d, opt)
                        time.sleep(1)
                        log.info("  Đã chọn 50/100 rows per page")
                        return True
    except Exception as e:
        log.debug(f"Không chỉnh được pagination dropdown: {e}")
    return False


def _get_all_table_rows_from_all_pages(d) -> list[list[str]]:
    """
    Duyệt qua tất cả các trang pagination của bảng MUI để lấy toàn bộ dữ liệu cell hiển thị.
    """
    time.sleep(1.5)

    all_rows = []
    seen_row_sigs = set()
    page_count = 0
    max_pages = 15

    while page_count < max_pages:
        page_count += 1
        rows_elements = d.find_elements(By.XPATH,
            '//div[contains(@class,"MuiTabPanel") and not(@hidden) and not(contains(@style,"display: none"))]//tbody/tr'
            ' | //div[@role="tabpanel" and not(@hidden) and not(contains(@style,"display: none"))]//tbody/tr'
            ' | //div[contains(@class,"MuiPaper-root") and not(contains(@style,"display: none"))]//tbody/tr'
            ' | //tbody/tr'
        )
        current_page_added = 0
        for row in rows_elements:
            try:
                cells = row.find_elements(By.XPATH, './td | ./th | ./div[@role="cell" or @role="gridcell"]')
                if cells:
                    row_texts = []
                    for c in cells:
                        t = (c.text or c.get_attribute("textContent") or "").strip()
                        row_texts.append(t)
                    
                    sig = " || ".join(row_texts)
                    # Bỏ qua các dòng trống hoàn toàn
                    if sig and any(t for t in row_texts) and sig not in seen_row_sigs:
                        seen_row_sigs.add(sig)
                        all_rows.append(row_texts)
                        current_page_added += 1
            except Exception:
                pass

        next_btns = d.find_elements(By.XPATH,
            '//button[@aria-label="Go to next page" or @title="Next page" or contains(@aria-label,"next page") or contains(@aria-label,"Next page")]'
            ' | //button[contains(@class,"MuiIconButton-root") and .//*[contains(@data-testid,"KeyboardArrowRight")]]'
            ' | //button[not(@disabled) and .//*[contains(@data-testid,"KeyboardArrowRight")]]'
        )

        clicked_next = False
        for btn in next_btns:
            try:
                if btn.is_displayed() and btn.is_enabled():
                    aria_dis = btn.get_attribute("aria-disabled") or btn.get_attribute("disabled")
                    if aria_dis and aria_dis.lower() in ("true", "disabled"):
                        continue
                    _js_click(d, btn)
                    time.sleep(1.5)
                    clicked_next = True
                    break
            except Exception:
                pass

        if not clicked_next or current_page_added == 0:
            break

    log.info(f"  _get_all_table_rows_from_all_pages: Scraped {len(all_rows)} rows across {page_count} pages.")
    return all_rows


def _find_header_indices(d) -> dict:
    """Tự động quét các header <th> hiển thị để tìm vị trí các cột."""
    indices = {
        "vao_mang": -1,
        "ra_mang": -1,
        "thoi_gian": -1,
        "trang_thai": -1,
        "nguyen_nhan": -1,
        "rx_power": -1,
    }
    try:
        header_els = d.find_elements(By.XPATH, '//thead//th | //div[@role="columnheader"]')
        visible_headers = []
        for h in header_els:
            if h.is_displayed():
                visible_headers.append(h.text.strip().lower())
        
        for idx, text in enumerate(visible_headers):
            if "vào mạng" in text or "vao mang" in text:
                indices["vao_mang"] = idx
            elif "ra mạng" in text or "ra mang" in text:
                indices["ra_mang"] = idx
            elif "thời gian" in text or "thoi gian" in text:
                indices["thoi_gian"] = idx
            elif "trạng thái" in text or "trang thai" in text:
                indices["trang_thai"] = idx
            elif "nguyên nhân" in text or "nguyen nhan" in text:
                indices["nguyen_nhan"] = idx
            elif "công suất" in text or "cong suat" in text or "rx" in text or "power" in text or "suy hao" in text:
                indices["rx_power"] = idx
        log.info(f"  Dynamic table header indices: {indices}")
    except Exception as e:
        log.warning(f"  Lỗi khi quét header indices: {e}")
    return indices


def _click_sub_tab(d, keyword: str) -> bool:
    """Click sub-tab pill/button bên dưới tab 'Các lần kết nối'."""
    xpaths = [
        f'//button[contains(normalize-space(.),"{keyword}")]',
        f'//div[contains(@class,"MuiButton") and contains(normalize-space(.),"{keyword}")]',
        f'//span[contains(normalize-space(.),"{keyword}")]/ancestor::button',
        f'//*[contains(normalize-space(.),"{keyword}")]',
    ]
    for xpath in xpaths:
        try:
            for el in d.find_elements(By.XPATH, xpath):
                if el.is_displayed():
                    _js_click(d, el)
                    time.sleep(1.5)
                    log.debug(f"  Clicked sub-tab: {keyword}")
                    return True
        except Exception:
            pass
    log.warning(f"  Không click được sub-tab: {keyword}")
    return False


def _parse_dt(date_str: str):
    if not date_str:
        return None
    from datetime import datetime
    import re

    date_str = str(date_str).strip()
    date_str = date_str.replace(',', '').replace('  ', ' ')

    formats = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except Exception:
            pass

    m = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', date_str)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    m2 = re.search(r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})', date_str)
    if m2:
        try:
            return datetime.strptime(m2.group(1), "%d/%m/%Y %H:%M:%S")
        except Exception:
            pass

    return None


def _cross_check_disconnections(d, tg_hoan_tat_str: str) -> str:
    """
    Đối chiếu 1: Sub-tab 'Các lần kết nối' và 'Nguyên nhân rớt kết nối OLT'.
    - Lọc chính xác các sự cố XẢY RA SAU THỜI GIAN HOÀN TẤT (tg_hoan_tat).
    - Sub-tab 1 'Các lần kết nối': Lọc Cột 'Ra mạng' > tg_hoan_tat (Bỏ qua '--' / đang Online).
    - Sub-tab 2 'Nguyên nhân rớt kết nối OLT': Lọc Cột 'Thời gian' > tg_hoan_tat VÀ Trạng thái == 'Offline', trích xuất Nguyên nhân OLT.
    """
    _click_mui_tab(d, "Các lần kết nối")
    time.sleep(1.5)

    t0 = _parse_dt(tg_hoan_tat_str)
    log.info(f"  Thời gian hoàn tất (t0): {t0} (Gốc: '{tg_hoan_tat_str}')")

    # 1. Sub-tab 'Các lần kết nối'
    _click_sub_tab(d, "Các lần kết nối")
    hdr_conn = _find_header_indices(d)
    ra_idx = hdr_conn["ra_mang"]
    vao_idx = hdr_conn["vao_mang"]

    rows_conn = _get_all_table_rows_from_all_pages(d)

    ra_mang_events = set()
    for row_cells in rows_conn:
        ra_mang_val = ""
        if ra_idx != -1 and ra_idx < len(row_cells):
            ra_mang_val = row_cells[ra_idx]
        elif len(row_cells) >= 5:
            ra_mang_val = row_cells[4]

        # Kiểm tra xem cell có phải timestamp 'Ra mạng' hợp lệ hay không
        if not ra_mang_val or ra_mang_val in ("--", "-", "") or "Vào mạng" in ra_mang_val or "vào mạng" in ra_mang_val:
            continue

        # Đảm bảo không trùng với cell 'Vào mạng'
        if vao_idx != -1 and vao_idx < len(row_cells) and row_cells[vao_idx] == ra_mang_val:
            continue

        ev_dt = _parse_dt(ra_mang_val)
        if ev_dt:
            if t0:
                if ev_dt > t0:
                    ra_mang_events.add(str(ev_dt))
            else:
                ra_mang_events.add(str(ev_dt))

    # 2. Sub-tab 'Nguyên nhân rớt kết nối OLT' (Chờ 10s để OLT load dữ liệu)
    log.info("  Chờ 10s để OLT load dữ liệu...")
    _click_sub_tab(d, "Nguyên nhân rớt kết nối OLT")
    time.sleep(10)

    hdr_olt = _find_header_indices(d)
    time_idx = hdr_olt["thoi_gian"] if hdr_olt["thoi_gian"] != -1 else 0
    status_idx = hdr_olt["trang_thai"] if hdr_olt["trang_thai"] != -1 else 1
    cause_idx = hdr_olt["nguyen_nhan"] if hdr_olt["nguyen_nhan"] != -1 else 2

    rows_olt = _get_all_table_rows_from_all_pages(d)

    olt_cause = "Bình thường / Không rõ"
    olt_offline_events_after_t0 = set()

    for row_cells in rows_olt:
        time_str = row_cells[time_idx] if time_idx < len(row_cells) else ""
        status_str = row_cells[status_idx] if status_idx < len(row_cells) else ""
        cause_str = row_cells[cause_idx] if cause_idx < len(row_cells) else ""
        row_str = " | ".join(row_cells)

        ev_dt = _parse_dt(time_str)
        is_after_t0 = True if (not t0 or (ev_dt and ev_dt > t0)) else False
        is_offline = True if ("Offline" in status_str or "offline" in status_str or "OFFLINE" in status_str) else False

        if is_after_t0 and is_offline:
            if ev_dt:
                olt_offline_events_after_t0.add(str(ev_dt))
            else:
                olt_offline_events_after_t0.add(time_str)

            # Trích xuất nguyên nhân OLT của đợt rớt sau hoàn tất
            if cause_str and cause_str.strip() not in ("-", "--", "None", "nan", ""):
                if olt_cause == "Bình thường / Không rõ":
                    olt_cause = cause_str.strip()
            else:
                for token in ["POWER_OFF", "LOSi", "LINK_DOWN", "DYING_GASP", "SYSTEM_RESET"]:
                    if token in row_str:
                        if olt_cause == "Bình thường / Không rõ":
                            olt_cause = token
                        break

    # Tổng hợp số lần rớt mạng sau khi hoàn tất (Cột 'Ra Mạng' > TG Hoàn Tất)
    ra_mang_count = len(ra_mang_events)

    if ra_mang_count > 2:
        return f"Chưa đảm bảo (Phát hiện {ra_mang_count} lần Ra Mạng từ mốc hoàn tất - NN OLT: {olt_cause})"
    elif ra_mang_count == 2:
        if "POWER_OFF" in olt_cause.upper() or olt_cause == "POWER_OFF":
            return "Đảm bảo (Sau Xử Lý rớt 2 lần do POWER_OFF)"
        else:
            return f"Chưa đảm bảo (Phát hiện 2 lần Ra Mạng từ mốc hoàn tất - NN OLT: {olt_cause})"
    elif ra_mang_count == 1:
        return "Đảm bảo (Sau Xử Lý chỉ ra Mạng 1 Lần)"
    else:
        return "Đảm bảo (Kết nối ổn định)"


def _cross_check_tap_diem(d) -> str:
    """
    Đối chiếu 2: Sub-tab 'Hợp đồng cùng tập điểm'.
    - Chờ 3.5s để đọc dữ liệu hoàn tất.
    - Quét toàn bộ các trang để lấy Total HĐ (x HĐ).
    - Đếm số HĐ Online/Offline.
    - Đếm số HĐ Đạt (> -23.5dBm), Không đạt (<= -23.5dBm) và Unknown (Không rõ/N/A).
    - Trích xuất danh sách HĐ Không đạt & HĐ Unknown & HĐ Offline.
    - Đưa ra kết luận chi tiết.
    """
    _click_mui_tab(d, "Các lần kết nối")
    time.sleep(1)
    _click_sub_tab(d, "Hợp đồng cùng tập điểm")
    time.sleep(3.5)

    rows_tap = _get_all_table_rows_from_all_pages(d)

    import re

    total_tap = len(rows_tap)
    if total_tap == 0:
        return "Tập điểm: Không đọc được danh sách HĐ cùng tập điểm"

    online_count = 0
    offline_count = 0
    khong_dat_count = 0
    dat_count = 0
    unknown_count = 0

    offline_hds = []
    khong_dat_hds = []
    unknown_hds = []

    hdr_tap = _find_header_indices(d)
    status_idx = hdr_tap.get("trang_thai", -1)
    pwr_idx = hdr_tap.get("rx_power", -1)

    for r_cells in rows_tap:
        row_str = " | ".join(r_cells)

        # 1. Lấy mã HĐ (ví dụ SG... từ cell)
        hd_code = ""
        for cell in r_cells:
            m_hd = re.search(r'\b(SG[A-Z0-9]{6,10})\b', cell, re.IGNORECASE)
            if m_hd:
                hd_code = m_hd.group(1).upper()
                break

        # 2. Xác định Trạng thái Online / Offline
        is_online = False
        is_offline = False
        row_lower = row_str.lower()

        if "online" in row_lower or "trực tuyến" in row_lower:
            is_online = True
        elif "offline" in row_lower or "ngoại tuyến" in row_lower or "mất kết nối" in row_lower:
            is_offline = True
        elif status_idx != -1 and status_idx < len(r_cells):
            st_val = r_cells[status_idx].lower()
            if "on" in st_val:
                is_online = True
            elif "off" in st_val:
                is_offline = True

        if is_online:
            online_count += 1
        elif is_offline:
            offline_count += 1
            if hd_code and hd_code not in offline_hds:
                offline_hds.append(hd_code)
        else:
            # Mặc định tính online nếu không có dấu hiệu offline
            online_count += 1

        # 3. Xác định Công suất thu Rx Power & Trạng thái Unknown
        pwr_cell_text = ""
        if pwr_idx != -1 and pwr_idx < len(r_cells):
            pwr_cell_text = r_cells[pwr_idx]
        else:
            for cell in r_cells:
                c_low = cell.lower()
                if "dbm" in c_low or "db" in c_low or "unknown" in c_low:
                    pwr_cell_text = cell
                    break
            if not pwr_cell_text:
                for cell in r_cells:
                    if re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', cell):
                        continue
                    if re.search(r'[0-9a-fA-F]{2}[:\-][0-9a-fA-F]{2}', cell):
                        continue
                    m_val = re.search(r'[-+]?\d+\.\d+|[-+]?\d+', cell)
                    if m_val:
                        v = float(m_val.group(0))
                        if 5.0 <= abs(v) <= 45.0:
                            pwr_cell_text = cell
                            break

        pwr_lower = pwr_cell_text.lower()
        if "unknown" in pwr_lower or "không rõ" in pwr_lower or "none" in pwr_lower:
            unknown_count += 1
            if hd_code and hd_code not in unknown_hds:
                unknown_hds.append(hd_code)
        elif pwr_cell_text:
            m_num = re.search(r'[-+]?\d+\.\d+|[-+]?\d+', pwr_cell_text)
            if m_num:
                try:
                    val = float(m_num.group(0))
                    val_pwr = -abs(val)
                    if val_pwr <= -23.5:
                        khong_dat_count += 1
                        if hd_code and not any(hd_code in x for x in khong_dat_hds):
                            khong_dat_hds.append(f"{hd_code}: {val_pwr:.2f}dBm")
                    else:
                        dat_count += 1
                except Exception:
                    pass

    offline_pct = (offline_count / total_tap) * 100 if total_tap > 0 else 0
    offline_str = f" ({', '.join(offline_hds[:3])})" if offline_hds else ""
    khong_dat_str = f" ({', '.join(khong_dat_hds[:3])})" if khong_dat_hds else ""
    unknown_str = f"; {unknown_count}/{total_tap} HĐ Unknown ({', '.join(unknown_hds[:3])})" if unknown_hds else ""

    # TH1: Offline >= 50% tổng số HĐ trong tập điểm >> Cảnh báo tập điểm
    if offline_pct >= 50.0:
        return f"Cảnh báo tập điểm (Tập điểm này có {total_tap} HĐ; {offline_count}/{total_tap} Offline (>=50%){offline_str}; {khong_dat_count}/{total_tap} Rx Power <= -23.5dBm{khong_dat_str}{unknown_str})"

    # TH2: Offline < 50% tổng số HĐ
    if khong_dat_count > 0:
        return f"Yêu cầu xử lý Rx Power (Tập điểm này có {total_tap} HĐ; {khong_dat_count}/{total_tap} HĐ Rx Power không đạt <= -23.5dBm{khong_dat_str}; {online_count}/{total_tap} Online, {offline_count}/{total_tap} Offline{offline_str}{unknown_str})"
    elif unknown_count > 0:
        return f"Tập điểm có HĐ Unknown (Tập điểm này có {total_tap} HĐ; {online_count}/{total_tap} Online, {offline_count}/{total_tap} Offline{offline_str}{unknown_str}; 0/{total_tap} suy hao)"
    elif offline_count > 0:
        return f"Tập điểm có HĐ Offline (Tập điểm này có {total_tap} HĐ; {online_count}/{total_tap} Online, {offline_count}/{total_tap} Offline{offline_str}; 0/{total_tap} suy hao)"
    else:
        return f"Tập điểm bình thường (Tập điểm này có {total_tap} HĐ; {online_count}/{total_tap} Online; Rx Power đạt)"




