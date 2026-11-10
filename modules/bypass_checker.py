"""
modules/bypass_checker.py - Module Quản lý và Hậu kiểm Bypass từ Google Sheet
Hỗ trợ:
1. Đọc danh sách Bypass từ Google Sheet CSV (SHEET ID: 1Sw-qSbLzvVdcW4rj7ywEZBEzKI6w5M82EQeYJUpdx7M, GID: 0)
2. Tra cứu công suất thu (suy hao) của hợp đồng trên MYBAE (management.mypt.vn)
3. Quy tắc đánh giá chuẩn:
   - Công suất <= -23.5 dBm: KHÔNG ĐẠT và note suy hao vào cột Suy Hao Hậu Kiểm
   - Suy hao -10 dBm đến -23.5 dBm (-23.5 < val <= -10.0): ĐẠT và note suy hao vào cột Suy Hao Hậu Kiểm
   - Suy hao > -10 dBm đến dương (hoặc mất quang 0.0 dBm): Mất tín hiệu - KHÔNG ĐẠT và note "Mất Tín Hiệu"
4. Đồng bộ tức thời lên Google Sheet qua Google Apps Script Webhook
"""
import io
import re
import time
import logging
import threading
import requests
import pandas as pd
from datetime import datetime

import config
from modules.auth import get_driver, is_logged_in, _try_load_session
from modules.analyzer import (
    _driver_lock,
    _go_check_contract,
    _enter_and_analyze,
    _wait_results,
    _check_and_handle_reboot_popup,
    _click_mui_tab,
    _get_body_lines,
    _find_value_after_label,
    _exit_contract
)

log = logging.getLogger("bypass_checker")

DEFAULT_BYPASS_SHEET_ID = getattr(config, "BYPASS_SHEET_ID", "1Sw-qSbLzvVdcW4rj7ywEZBEzKI6w5M82EQeYJUpdx7M")
DEFAULT_BYPASS_SHEET_GID = getattr(config, "BYPASS_SHEET_GID", "0")

# ──────────────────────────────────────────────
#  CÁC QUY TẮC ĐÁNH GIÁ CÔNG SUẤT THU (RX POWER)
# ──────────────────────────────────────────────
def evaluate_bypass_power(raw_power: str) -> tuple[str, str, float | None]:
    """
    Quy tắc hậu kiểm bypass theo yêu cầu:
    - Công suất nếu <= -23.5 dBm thì KHÔNG ĐẠT và note suy hao vào cột Suy Hao Hậu Kiểm
    - Nếu suy hao -10 dBm đến -23.5 dBm : ĐẠT và note suy hao vào cột Suy Hao Hậu Kiểm
    - Nếu suy hao > -10 dBm đến dương thì : Mất tín hiệu - KHÔNG ĐẠT và note "Mất Tín Hiệu"

    Returns: (hau_kiem, suy_hao_note, val_float)
    """
    if not raw_power or str(raw_power).strip() in ("", "—", "None", "nan", "null", "Unknown"):
        return "KHÔNG ĐẠT", "Mất Tín Hiệu", None

    s = str(raw_power).strip()
    s_low = s.lower()

    if "mất tín hiệu" in s_low or "đứt cáp" in s_low or "mất kết nối" in s_low:
        return "KHÔNG ĐẠT", "Mất Tín Hiệu", 0.0

    # Trích xuất số thực đầu tiên
    m = re.search(r'[-+]?\d+(?:[.,]\d+)?', s)
    if not m:
        return "KHÔNG ĐẠT", "Mất Tín Hiệu", None

    try:
        val = float(m.group(0).replace(",", "."))
    except Exception:
        return "KHÔNG ĐẠT", "Mất Tín Hiệu", None

    # Xét các khoảng giá trị:
    # 1. > -10.0 dBm đến dương (bao gồm 0.0 dBm, số dương hoặc tín hiệu quá cao bất thường)
    if val > -10.0:
        return "KHÔNG ĐẠT", "Mất Tín Hiệu", val

    # 2. Khoảng đạt chuẩn: -10.0 dBm đến -23.5 dBm (-23.5 < val <= -10.0)
    elif -23.5 < val <= -10.0:
        return "ĐẠT", f"{val:.2f} dBm", val

    # 3. Suy hao cao: <= -23.5 dBm
    else:
        return "KHÔNG ĐẠT", f"{val:.2f} dBm", val


# ──────────────────────────────────────────────
#  TRUY VẤN VÀ PARSE GOOGLE SHEET CSV
# ──────────────────────────────────────────────
def fetch_bypass_data(sheet_id: str = None, gid: str = None) -> dict:
    """
    Tải dữ liệu mới nhất từ Google Sheet dạng CSV và trích xuất danh sách hợp đồng Bypass.
    """
    sid = sheet_id or getattr(config, "BYPASS_SHEET_ID", DEFAULT_BYPASS_SHEET_ID)
    sgid = gid or getattr(config, "BYPASS_SHEET_GID", DEFAULT_BYPASS_SHEET_GID)

    csv_url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={sgid}"
    log.info(f"Đang tải dữ liệu Google Sheet Bypass từ: {csv_url}")

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        resp = requests.get(csv_url, headers=headers, timeout=25, allow_redirects=True)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} khi tải Google Sheet")
    except Exception as e:
        log.error(f"Lỗi tải sheet bypass: {e}")
        return {
            "success": False,
            "error": str(e),
            "rows": [],
            "stats": {}
        }

    try:
        df = pd.read_csv(io.StringIO(resp.text), dtype=str)
    except Exception as e:
        log.error(f"Lỗi phân tích CSV từ Google Sheet: {e}")
        return {
            "success": False,
            "error": f"Lỗi đọc định dạng bảng tính: {e}",
            "rows": [],
            "stats": {}
        }

    # Chuẩn hóa tên cột (loại bỏ khoảng trắng đầu/cuối)
    df.columns = [str(c).strip() for c in df.columns]

    # Tìm các cột cần thiết (dựa theo thứ tự hoặc tên cột)
    # Cấu trúc: ID | Tác Vụ | Email | MSNV | Account Inside | SHD | Nguyên Nhân 1 | Nguyên Nhân 2 | Ngày Cập Nhật | Thao Tác | Hậu kiểm | Suy Hao Hậu Kiểm
    col_names = list(df.columns)

    def get_col_val(row, col_idx, expected_names):
        for name in expected_names:
            if name in col_names:
                val = row.get(name)
                if pd.notna(val):
                    return str(val).strip()
        if len(row) > col_idx:
            val = row.iloc[col_idx]
            if pd.notna(val):
                return str(val).strip()
        return ""

    rows = []
    stats = {
        "total": len(df),
        "empty_count": 0,
        "completed_count": 0,
        "dat_count": 0,
        "khong_dat_count": 0,
        "mat_tin_hieu_count": 0
    }

    for idx, row in df.iterrows():
        sheet_row_num = idx + 2  # Dòng trong Google Sheet (Header là dòng 1)

        row_id = get_col_val(row, 0, ["ID", "Id", "id"])
        tac_vu = get_col_val(row, 1, ["Tác Vụ", "Tác Vụ", "Tac Vu"])
        email = get_col_val(row, 2, ["Email", "email"])
        msnv = get_col_val(row, 3, ["MSNV", "msnv"])
        account_inside = get_col_val(row, 4, ["Account Inside", "Inside Account", "Inside"])
        shd = get_col_val(row, 5, ["SHD", "Số HĐ", "Hợp đồng", "So HD"])
        nguyen_nhan_1 = get_col_val(row, 6, ["Nguyên Nhân 1", "Nguyen Nhan 1"])
        nguyen_nhan_2 = get_col_val(row, 7, ["Nguyên Nhân 2", "Nguyen Nhan 2"])
        ngay_cap_nhat = get_col_val(row, 8, ["Ngày Cập Nhật", "Ngày Cập Nhật", "Ngay Cap Nhat"])
        thao_tac = get_col_val(row, 9, ["Thao Tác", "Thao Tac"])
        hau_kiem = get_col_val(row, 10, ["Hậu kiểm", "Hau kiem", "Hậu Kiểm"])
        suy_hao = get_col_val(row, 11, ["Suy Hao Hậu Kiểm", "Suy hao hậu kiểm", "Suy Hao"])

        # Kiểm tra trạng thái rỗng của Hậu kiểm
        is_empty = (not hau_kiem) or (hau_kiem.lower() in ("nan", "none", "null", "—", "-"))

        if is_empty:
            stats["empty_count"] += 1
            status_badge = "pending"
        else:
            stats["completed_count"] += 1
            if "không đạt" in hau_kiem.lower():
                stats["khong_dat_count"] += 1
                if "mất tín hiệu" in suy_hao.lower():
                    stats["mat_tin_hieu_count"] += 1
                status_badge = "khong_dat"
            elif "đạt" in hau_kiem.lower():
                stats["dat_count"] += 1
                status_badge = "dat"
            else:
                status_badge = "other"

        rows.append({
            "row_num": sheet_row_num,
            "id": row_id or str(idx + 1),
            "tac_vu": tac_vu,
            "email": email,
            "msnv": msnv,
            "account_inside": account_inside,
            "shd": shd,
            "nguyen_nhan_1": nguyen_nhan_1,
            "nguyen_nhan_2": nguyen_nhan_2,
            "ngay_cap_nhat": ngay_cap_nhat,
            "thao_tac": thao_tac,
            "hau_kiem": "" if is_empty else hau_kiem,
            "suy_hao": suy_hao if pd.notna(suy_hao) else "",
            "is_empty": is_empty,
            "status_badge": status_badge,
            "sync_status": "synced" if not is_empty else "pending"
        })

    return {
        "success": True,
        "sheet_id": sid,
        "sheet_gid": sgid,
        "total_rows": len(rows),
        "stats": stats,
        "rows": rows,
        "fetched_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    }


# ──────────────────────────────────────────────
#  TRA CỨU CÔNG SUẤT HỢP ĐỒNG TRÊN MYBAE
# ──────────────────────────────────────────────
def check_single_contract_power(contract_number: str, step_callback=None) -> dict:
    """
    Thực hiện tra cứu MYBAE siêu tốc cho 1 hợp đồng để lấy thông số Công suất thu (Rx Power)
    và đánh giá Hậu Kiểm Bypass.
    """
    clean_hd = str(contract_number).strip().upper()
    if not clean_hd:
        return {
            "shd": contract_number,
            "cong_suat_thu": "",
            "hau_kiem": "KHÔNG ĐẠT",
            "suy_hao": "Mất Tín Hiệu",
            "val_pwr": None,
            "status": "error",
            "error": "Số hợp đồng rỗng"
        }

    with _driver_lock:
        d = get_driver(headless=True)
        try:
            if not is_logged_in():
                _try_load_session(d)
        except Exception as e:
            log.warning(f"Kiểm tra session trước khi tra cứu Bypass HĐ {clean_hd}: {e}")

        res = {
            "shd": clean_hd,
            "cong_suat_thu": "",
            "hau_kiem": "",
            "suy_hao": "",
            "val_pwr": None,
            "status": "ok",
            "error": ""
        }

        try:
            log.info(f"[{clean_hd}] 🔍 Bắt đầu tra cứu MYBAE cho Hậu kiểm Bypass...")
            if step_callback:
                step_callback(f"🔎 Tra cứu thông tin hợp đồng {clean_hd}", 1)

            # 1. Mở trang check-contract
            _go_check_contract(d)

            # 2. Nhập số HĐ và bấm Phân tích
            if step_callback:
                step_callback(f"📝 Nhập hợp đồng {clean_hd} & Gửi yêu cầu phân tích", 2)
            _enter_and_analyze(d, clean_hd)

            # 3. Kiểm tra popup Reboot modem nếu có
            did_reboot = _check_and_handle_reboot_popup(d, timeout_check=4)
            if did_reboot:
                log.info(f"[{clean_hd}] Đã xử lý popup reboot modem, bấm phân tích lại...")
                time.sleep(2)

            # 4. Chờ kết quả chẩn đoán trả về
            if step_callback:
                step_callback(f"⏳ Chờ hệ thống MYBAE trả kết quả chẩn đoán", 3)
            _wait_results(d)

            # 5. Chuyển sang tab "Thông số hệ thống" để đọc công suất thu
            if step_callback:
                step_callback(f"⚡ Đọc thông số công suất thu (Rx Power)", 4)
            _click_mui_tab(d, "Thông số hệ thống")
            time.sleep(0.5)

            lines_sys = _get_body_lines(d)
            cs_thu = (
                _find_value_after_label(lines_sys, "Công suất thu") or
                _find_value_after_label(lines_sys, "Cong suat thu")
            )
            res["cong_suat_thu"] = cs_thu or ""
            log.info(f"[{clean_hd}] Đọc được công suất thu: '{cs_thu}'")

            # 6. Đánh giá Hậu Kiểm & Suy Hao Hậu Kiểm theo quy tắc chuẩn
            hau_kiem, suy_hao_note, val_pwr = evaluate_bypass_power(cs_thu)
            res["hau_kiem"] = hau_kiem
            res["suy_hao"] = suy_hao_note
            res["val_pwr"] = val_pwr

            if step_callback:
                step_callback(f"✅ Hoàn tất đánh giá: {hau_kiem} ({suy_hao_note})", 5)
            log.info(f"[{clean_hd}] => Kết luận: {hau_kiem} | {suy_hao_note}")

        except Exception as e:
            log.error(f"[{clean_hd}] ❌ Lỗi tra cứu MYBAE: {e}", exc_info=True)
            res["status"] = "error"
            res["error"] = str(e)
            # Theo quy tắc: nếu lỗi hoặc không có tín hiệu thì đánh giá Không Đạt - Mất tín hiệu
            res["hau_kiem"] = "KHÔNG ĐẠT"
            res["suy_hao"] = "Mất Tín Hiệu"

        finally:
            log.info(f"[{clean_hd}] Thoát HĐ...")
            _exit_contract(d)

        return res


# ──────────────────────────────────────────────
#  ĐỒNG BỘ LÊN GOOGLE SHEET QUA WEBHOOK APPS SCRIPT
# ──────────────────────────────────────────────
def update_google_sheet_via_webhook(webhook_url: str, updates: list) -> tuple[bool, str]:
    """
    Gửi dữ liệu cập nhật lên Google Sheet thông qua Webhook URL của Google Apps Script.
    updates là danh sách các dict dạng:
    [
        {"row": 2, "id": "1", "shd": "SGh263406", "hau_kiem": "ĐẠT", "suy_hao": "-19.20 dBm"},
        ...
    ]
    """
    if not webhook_url or not str(webhook_url).strip():
        return False, "Chưa cấu hình URL Webhook của Google Apps Script"

    url = str(webhook_url).strip()
    log.info(f"Đang gửi {len(updates)} bản ghi cập nhật lên Google Sheet qua Webhook...")

    payload = {"updates": updates}
    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"},
                             timeout=30, allow_redirects=True)
        if resp.status_code == 200:
            try:
                res_json = resp.json()
                if res_json.get("status") == "success":
                    return True, f"✅ Đã đồng bộ thành công {res_json.get('updated', len(updates))} dòng lên Google Sheet!"
                else:
                    return False, f"Google Apps Script báo lỗi: {res_json.get('message', 'Không rõ')}"
            except Exception:
                return True, "✅ Đã gửi yêu cầu đồng bộ thành công lên Google Sheet"
        else:
            return False, f"Lỗi HTTP {resp.status_code} từ Webhook"
    except Exception as e:
        log.error(f"Lỗi khi gửi webhook Google Sheet: {e}")
        return False, f"Lỗi kết nối Webhook: {e}"


def test_webhook_connection(webhook_url: str) -> tuple[bool, str]:
    """Kiểm tra phản hồi của Google Apps Script Webhook."""
    if not webhook_url or not str(webhook_url).strip():
        return False, "Vui lòng nhập URL Webhook"

    url = str(webhook_url).strip()
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True)
        if resp.status_code == 200:
            return True, "✅ Kết nối Webhook Google Apps Script thành công và phản hồi tốt!"
        return False, f"Webhook phản hồi mã lỗi HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Không thể kết nối đến Webhook: {e}"


# ──────────────────────────────────────────────
#  QUẢN LÝ TIẾN TRÌNH CHẠY HẬU KIỂM NỀN (WORKER)
# ──────────────────────────────────────────────
class BypassManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._is_running = False
        self._cancel_requested = False
        self._thread = None
        self._state = {
            "is_running": False,
            "total": 0,
            "current_index": 0,
            "current_shd": "",
            "current_step": "",
            "step_num": 0,
            "percent": 0,
            "completed": 0,
            "success_count": 0,
            "dat_count": 0,
            "khong_dat_count": 0,
            "mat_tin_hieu_count": 0,
            "synced_count": 0,
            "logs": [],
            "results": [],
            "start_time": "",
            "end_time": "",
            "error": ""
        }

    def get_status(self):
        with self._lock:
            return dict(self._state)

    def is_running(self):
        with self._lock:
            return self._is_running

    def request_cancel(self):
        with self._lock:
            if self._is_running:
                self._cancel_requested = True
                self._state["current_step"] = "Đang dừng tiến trình..."
                return True
            return False

    def start_check(self, sheet_id=None, gid=None, webhook_url=None, only_empty=True, selected_rows=None):
        with self._lock:
            if self._is_running:
                return False, "Tiến trình Hậu kiểm Bypass đang chạy, vui lòng chờ hoàn tất."

            self._is_running = True
            self._cancel_requested = False
            self._state = {
                "is_running": True,
                "total": 0,
                "current_index": 0,
                "current_shd": "",
                "current_step": "Khởi tạo và tải dữ liệu từ Google Sheet...",
                "step_num": 1,
                "percent": 0,
                "completed": 0,
                "success_count": 0,
                "dat_count": 0,
                "khong_dat_count": 0,
                "mat_tin_hieu_count": 0,
                "synced_count": 0,
                "logs": [f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Bắt đầu tiến trình Hậu kiểm Bypass..."],
                "results": [],
                "start_time": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "end_time": "",
                "error": ""
            }

        self._thread = threading.Thread(
            target=self._worker,
            args=(sheet_id, gid, webhook_url, only_empty, selected_rows),
            daemon=True
        )
        self._thread.start()
        return True, "✅ Đã khởi chạy tiến trình Hậu kiểm Bypass thành công!"

    def _worker(self, sheet_id, gid, webhook_url, only_empty, selected_rows):
        log.info("Bắt đầu luồng kiểm tra Hậu Kiểm Bypass...")
        try:
            # 1. Tải dữ liệu từ Sheet
            sheet_data = fetch_bypass_data(sheet_id=sheet_id, gid=gid)
            if not sheet_data.get("success"):
                raise RuntimeError(sheet_data.get("error", "Không thể tải dữ liệu Google Sheet"))

            all_rows = sheet_data.get("rows", [])
            # Lọc các dòng cần chạy: Nếu only_empty=True thì chỉ chạy dòng có Hậu kiểm trống
            target_rows = []
            for r in all_rows:
                if selected_rows and r["row_num"] not in selected_rows and r["id"] not in selected_rows:
                    continue
                if only_empty:
                    if r.get("is_empty"):
                        target_rows.append(r)
                else:
                    target_rows.append(r)

            total = len(target_rows)
            with self._lock:
                self._state["total"] = total
                self._state["logs"].append(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Tìm thấy {total} hợp đồng cần hậu kiểm (Tổng số: {len(all_rows)} dòng)"
                )

            if total == 0:
                with self._lock:
                    self._state["current_step"] = "Không có hợp đồng nào cần hậu kiểm!"
                    self._state["is_running"] = False
                    self._state["end_time"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                return

            # 2. Quét và chạy MYBAE từng hợp đồng
            for idx, item in enumerate(target_rows, 1):
                if self._cancel_requested:
                    log.info("Tiến trình Hậu kiểm Bypass bị hủy bởi người dùng.")
                    with self._lock:
                        self._state["logs"].append(
                            f"[{datetime.now().strftime('%H:%M:%S')}] ⏹️ Tiến trình đã dừng lại theo yêu cầu."
                        )
                        self._state["current_step"] = "Đã dừng bởi người dùng."
                    break

                shd = item.get("shd", "").strip()
                row_num = item.get("row_num")
                item_id = item.get("id")

                with self._lock:
                    self._state["current_index"] = idx
                    self._state["current_shd"] = shd
                    self._state["step_num"] = 1
                    self._state["percent"] = int((idx - 1) / total * 100)
                    self._state["current_step"] = f"[{idx}/{total}] Đang tra cứu HĐ: {shd}..."

                def _step_cb(msg, snum):
                    with self._lock:
                        self._state["current_step"] = f"[{idx}/{total}] {shd}: {msg}"
                        self._state["step_num"] = snum

                # Tra cứu thông số MYBAE
                eval_res = check_single_contract_power(shd, step_callback=_step_cb)

                hau_kiem = eval_res.get("hau_kiem", "KHÔNG ĐẠT")
                suy_hao = eval_res.get("suy_hao", "Mất Tín Hiệu")
                cs_raw = eval_res.get("cong_suat_thu", "")

                # Đồng bộ tức thì lên Google Sheet qua Webhook (nếu có cấu hình)
                synced_ok = False
                sync_msg = "Chưa cấu hình Webhook"
                if webhook_url:
                    update_item = {
                        "row": row_num,
                        "id": item_id,
                        "shd": shd,
                        "hau_kiem": hau_kiem,
                        "suy_hao": suy_hao
                    }
                    ok, msg = update_google_sheet_via_webhook(webhook_url, [update_item])
                    synced_ok = ok
                    sync_msg = msg

                result_record = {
                    "row_num": row_num,
                    "id": item_id,
                    "shd": shd,
                    "account_inside": item.get("account_inside", ""),
                    "ngay_cap_nhat": item.get("ngay_cap_nhat", ""),
                    "cong_suat_thu": cs_raw,
                    "hau_kiem": hau_kiem,
                    "suy_hao": suy_hao,
                    "synced": synced_ok,
                    "sync_msg": sync_msg,
                    "time": datetime.now().strftime("%H:%M:%S")
                }

                with self._lock:
                    self._state["completed"] += 1
                    self._state["results"].append(result_record)
                    if hau_kiem == "ĐẠT":
                        self._state["dat_count"] += 1
                    else:
                        self._state["khong_dat_count"] += 1
                        if "Mất Tín Hiệu" in suy_hao:
                            self._state["mat_tin_hieu_count"] += 1

                    if synced_ok:
                        self._state["synced_count"] += 1

                    self._state["percent"] = int(idx / total * 100)
                    self._state["logs"].append(
                        f"[{datetime.now().strftime('%H:%M:%S')}] [{idx}/{total}] HĐ {shd}: {hau_kiem} ({suy_hao}) | {sync_msg}"
                    )

                time.sleep(1)

            # 3. Đồng bộ bù những ca chưa được ghi thành công (Batch Sync Fallback)
            if webhook_url and not self._cancel_requested:
                failed_items = [
                    {
                        "row": r["row_num"],
                        "id": r["id"],
                        "shd": r["shd"],
                        "hau_kiem": r["hau_kiem"],
                        "suy_hao": r["suy_hao"]
                    }
                    for r in self._state["results"] if not r.get("synced")
                ]
                if failed_items:
                    log.info(f"Đang đồng bộ bù {len(failed_items)} ca chưa ghi được lên Google Sheet...")
                    ok_retry, msg_retry = update_google_sheet_via_webhook(webhook_url, failed_items)
                    if ok_retry:
                        with self._lock:
                            self._state["synced_count"] += len(failed_items)
                            for r in self._state["results"]:
                                r["synced"] = True
                            self._state["logs"].append(
                                f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Đã đồng bộ bù thành công {len(failed_items)} ca lên Google Sheet."
                            )

            with self._lock:
                self._state["percent"] = 100
                self._state["current_step"] = "✅ Hoàn tất toàn bộ danh sách Hậu kiểm Bypass!"
                self._state["logs"].append(
                    f"[{datetime.now().strftime('%H:%M:%S')}] 🎉 Hoàn tất: Đạt {self._state['dat_count']} | Không đạt {self._state['khong_dat_count']} | Đồng bộ Sheet {self._state['synced_count']}/{self._state['completed']}"
                )

        except Exception as e:
            log.error(f"Lỗi ngoại lệ trong luồng Hậu kiểm Bypass: {e}", exc_info=True)
            with self._lock:
                self._state["error"] = str(e)
                self._state["current_step"] = f"Lỗi: {e}"
                self._state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Lỗi: {e}")

        finally:
            with self._lock:
                self._is_running = False
                self._state["is_running"] = False
                self._state["end_time"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            log.info("Luồng Hậu Kiểm Bypass đã kết thúc.")


# Instance Singleton quản lý tiến trình
bypass_manager = BypassManager()


# ──────────────────────────────────────────────
#  CƠ CHẾ AUTO HẬU KIỂM & ĐỒNG BỘ ĐỊNH KỲ (1 TIẾNG / LẦN)
# ──────────────────────────────────────────────
class AutoBypassScheduler:
    def __init__(self, manager: BypassManager):
        self._manager = manager
        self._lock = threading.Lock()
        self._enabled = False
        self._interval_minutes = 60  # Mặc định 1 tiếng / lần
        self._next_run_ts = 0
        self._last_run = ""
        self._next_run = ""
        self._last_result = ""
        self._thread = None
        self._stop_event = threading.Event()
        self._logs = []
        self._login_hook = None
        self._get_config_hook = None
        self._busy_hook = None

    def set_hooks(self, login_hook=None, get_config_hook=None, busy_hook=None):
        self._login_hook = login_hook
        self._get_config_hook = get_config_hook
        self._busy_hook = busy_hook

    def configure(self, enabled: bool = None, interval_minutes: int = None, immediate: bool = False):
        with self._lock:
            if interval_minutes is not None and interval_minutes > 0:
                self._interval_minutes = int(interval_minutes)
            if enabled is not None:
                self._enabled = bool(enabled)
                if self._enabled:
                    # Nếu chưa từng chạy lần nào (vừa khởi động hoặc vừa bật) hoặc yêu cầu quét sớm
                    if immediate or not self._last_run:
                        self._next_run_ts = time.time() + 15
                        self._next_run = datetime.fromtimestamp(self._next_run_ts).strftime("%H:%M:%S")
                        self._log(f"🟢 Đã BẬT cơ chế Auto Hậu Kiểm Bypass (Chu kỳ {self._interval_minutes} phút/lần). Sẽ quét lần đầu sau 15 giây...")
                    else:
                        self._next_run_ts = time.time() + (self._interval_minutes * 60)
                        self._next_run = datetime.fromtimestamp(self._next_run_ts).strftime("%H:%M:%S")
                        self._log(f"🟢 Đã BẬT cơ chế Auto Hậu Kiểm Bypass (Chu kỳ {self._interval_minutes} phút/lần). Lần quét tiếp theo: {self._next_run}")
                else:
                    self._next_run_ts = 0
                    self._next_run = ""
                    self._log("⏸️ Đã TẮT cơ chế Auto Hậu Kiểm Bypass.")

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            log.info("Khởi động luồng chạy ngầm Auto Bypass (1 tiếng/lần)...")

    def stop(self):
        self._stop_event.set()

    def _log(self, msg: str):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}
        self._logs.append(entry)
        if len(self._logs) > 100:
            self._logs = self._logs[-100:]
        log.info(f"[AutoBypass] {msg}")

    def get_status(self):
        with self._lock:
            now_ts = time.time()
            rem = max(0, int(self._next_run_ts - now_ts)) if (self._enabled and self._next_run_ts > 0) else 0
            return {
                "enabled": self._enabled,
                "interval_minutes": self._interval_minutes,
                "last_run": self._last_run,
                "next_run": self._next_run,
                "remaining_seconds": rem,
                "last_result": self._last_result,
                "is_running": self._manager.is_running(),
                "logs": list(self._logs)
            }

    def trigger_run_now(self):
        if self._manager.is_running():
            return False, "Tiến trình Hậu kiểm đang chạy, vui lòng chờ."
        with self._lock:
            # Dời lần chạy tự động tiếp theo sau 1 tiếng kể từ lần bấm chạy ngay
            if self._enabled:
                self._next_run_ts = time.time() + (self._interval_minutes * 60)
                self._next_run = datetime.fromtimestamp(self._next_run_ts).strftime("%H:%M:%S")
        threading.Thread(target=self._execute_cycle, kwargs={"manual": True}, daemon=True).start()
        return True, "🚀 Đã kích hoạt quét Sheet và Hậu kiểm Bypass ngay lập tức!"

    def _execute_cycle(self, manual=False):
        tag = "[Thủ công]" if manual else "[Định kỳ 1h]"
        self._log(f"⏰ {tag} Bắt đầu quét Google Sheet kiểm tra Hậu kiểm Bypass...")
        try:
            cfg = self._get_config_hook() if self._get_config_hook else {}
            sheet_id = cfg.get("bypass_sheet_id") or getattr(config, "BYPASS_SHEET_ID", DEFAULT_BYPASS_SHEET_ID)
            gid = cfg.get("bypass_sheet_gid") or getattr(config, "BYPASS_SHEET_GID", DEFAULT_BYPASS_SHEET_GID)
            webhook_url = cfg.get("bypass_webhook_url") or getattr(config, "BYPASS_WEBHOOK_URL", "")

            # 1. Đảm bảo đăng nhập
            if self._login_hook:
                ok_login, msg_login = self._login_hook(force_reset=False)
                if not ok_login:
                    self._log("Phiên MYBAE cần làm mới, tiến hành đăng nhập lại...")
                    ok_login, msg_login = self._login_hook(force_reset=True)
                    if not ok_login:
                        self._log(f"❌ Không thể đăng nhập MYBAE: {msg_login}")
                        with self._lock:
                            self._last_result = f"Lỗi đăng nhập: {msg_login}"
                        return

            # 2. Đọc Sheet
            sheet_data = fetch_bypass_data(sheet_id=sheet_id, gid=gid)
            if not sheet_data.get("success"):
                err = sheet_data.get("error", "Lỗi đọc Sheet")
                self._log(f"❌ Không đọc được Google Sheet: {err}")
                with self._lock:
                    self._last_result = f"Lỗi đọc Sheet: {err}"
                return

            rows = sheet_data.get("rows", [])
            empty_rows = [r for r in rows if r.get("is_empty")]

            if not empty_rows:
                msg = f"Đã quét {len(rows)} dòng trên Sheet: Tất cả đã có kết quả Hậu kiểm. Không có ca mới."
                self._log(f"✅ {msg}")
                with self._lock:
                    self._last_run = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                    self._last_result = msg
                return

            self._log(f"🔎 Phát hiện {len(empty_rows)} hợp đồng chưa Hậu kiểm. Bắt đầu chạy MYBAE và tự động cập nhật lên Sheet...")

            # 3. Khởi động tiến trình chạy
            ok, start_msg = self._manager.start_check(
                sheet_id=sheet_id,
                gid=gid,
                webhook_url=webhook_url,
                only_empty=True
            )
            if not ok:
                self._log(f"⚠️ {start_msg}")
                return

            # Chờ tiến trình chạy xong
            while self._manager.is_running():
                time.sleep(2)

            st = self._manager.get_status()
            res_summary = f"Hoàn tất: Đạt {st.get('dat_count', 0)} | Không đạt {st.get('khong_dat_count', 0)} (Mất tín hiệu {st.get('mat_tin_hieu_count', 0)}) | Đã đồng bộ Sheet: {st.get('synced_count', 0)}/{st.get('completed', 0)}"
            self._log(f"🎉 {res_summary}")
            with self._lock:
                self._last_run = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                self._last_result = res_summary

        except Exception as e:
            self._log(f"❌ Ngoại lệ trong chu kỳ Auto Bypass: {e}")
            with self._lock:
                self._last_result = f"Lỗi: {e}"

    def _loop(self):
        while not self._stop_event.is_set():
            now_ts = time.time()
            with self._lock:
                should_run = (self._enabled and self._next_run_ts > 0 and now_ts >= self._next_run_ts)

            if should_run:
                is_busy = False
                if self._busy_hook:
                    try:
                        is_busy = bool(self._busy_hook())
                    except Exception:
                        pass

                if is_busy or self._manager.is_running():
                    self._log("Tiến trình đang bận hoặc hệ thống đang thực hiện tác vụ khác, dời lịch quét lại 2 phút...")
                    with self._lock:
                        self._next_run_ts = time.time() + 120
                        self._next_run = datetime.fromtimestamp(self._next_run_ts).strftime("%H:%M:%S")
                else:
                    try:
                        self._execute_cycle()
                    finally:
                        # Sau khi hoàn tất chu kỳ, lên lịch lần chạy tiếp theo đúng chu kỳ (1 tiếng)
                        with self._lock:
                            if self._enabled:
                                self._next_run_ts = time.time() + (self._interval_minutes * 60)
                                self._next_run = datetime.fromtimestamp(self._next_run_ts).strftime("%H:%M:%S")
                                self._log(f"⏰ Chu kỳ kế tiếp dự kiến lúc: {self._next_run} (sau {self._interval_minutes} phút)")

            self._stop_event.wait(5)


auto_bypass_scheduler = AutoBypassScheduler(bypass_manager)


# ──────────────────────────────────────────────
#  MÃ NGUỒN GOOGLE APPS SCRIPT CHO NGƯỜI DÙNG
# ──────────────────────────────────────────────
GOOGLE_APPS_SCRIPT_TEMPLATE = """/**
 * GOOGLE APPS SCRIPT - TỰ ĐỘNG CẬP NHẬT HẬU KIỂM BYPASS TỪ TOOL MYBAE AUTO
 * =======================================================================
 * Hướng dẫn cài đặt trong 1 phút:
 * 1. Mở Google Sheet Bypass (ID: 1Sw-qSbLzvVdcW4rj7ywEZBEzKI6w5M82EQeYJUpdx7M)
 * 2. Trên thanh menu, chọn: Tiện ích mở rộng (Extensions) > Apps Script
 * 3. Xóa hết code cũ, dán toàn bộ đoạn mã này vào rồi bấm Lưu (Ctrl + S)
 * 4. Nhấn nút "Triển khai" (Deploy) màu xanh ở góc trên bên phải > "Triển khai mới" (New deployment)
 * 5. Tại cửa sổ hiện ra:
 *    - Chọn loại (Select type): Nhấn biểu tượng bánh răng > Chọn "Ứng dụng web" (Web app)
 *    - Mô tả (Description): Bypass Auto Webhook
 *    - Thực thi dưới dạng (Execute as): Tôi (Me - tài khoản Google của bạn)
 *    - Ai có quyền truy cập (Who has access): Bất kỳ ai (Anyone)  <-- BẮT BUỘC CHỌN MỤC NÀY
 * 6. Nhấn nút "Triển khai" (Deploy) -> Cấp quyền truy cập nếu Google yêu cầu
 * 7. Sao chép "URL ứng dụng web" (Web app URL) dán vào ô Cấu hình Webhook trên Tool MYBAE AUTO.
 */

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.tryLock(30000); // Khóa tránh xung đột khi ghi nhiều dòng đồng thời
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = ss.getSheetByName("Sheet1") || ss.getSheets()[0];
    
    var contents = JSON.parse(e.postData.contents);
    var updates = Array.isArray(contents) ? contents : (contents.updates || [contents]);
    
    var values = sheet.getDataRange().getValues();
    var updatedCount = 0;
    
    // Cột K (cột 11, 1-based): Hậu kiểm
    // Cột L (cột 12, 1-based): Suy Hao Hậu Kiểm
    for (var i = 0; i < updates.length; i++) {
      var item = updates[i];
      var targetRow = -1;
      
      // 1. Ưu tiên tìm theo số hàng (row index)
      if (item.row && item.row > 1 && item.row <= values.length) {
        var rowIdx = item.row - 1;
        var rowShd = String(values[rowIdx][5] || "").trim().toUpperCase();
        var rowId = String(values[rowIdx][0] || "").trim();
        if (!item.shd || rowShd === String(item.shd).trim().toUpperCase() || rowId === String(item.id).trim()) {
          targetRow = item.row;
        }
      }
      
      // 2. Tìm theo ID (Cột A - index 0) hoặc SHD (Cột F - index 5)
      if (targetRow === -1 && (item.id || item.shd)) {
        for (var r = 1; r < values.length; r++) {
          var curId = String(values[r][0] || "").trim();
          var curShd = String(values[r][5] || "").trim().toUpperCase();
          if ((item.id && curId === String(item.id).trim()) || 
              (item.shd && curShd === String(item.shd).trim().toUpperCase())) {
            targetRow = r + 1; // 1-based
            break;
          }
        }
      }
      
      if (targetRow > 1) {
        if (item.hau_kiem !== undefined) {
          var valHk = String(item.hau_kiem);
          var cellHk = sheet.getRange(targetRow, 11);
          cellHk.setValue(valHk);
          if (valHk.indexOf("ĐẠT") !== -1 && valHk.indexOf("KHÔNG") === -1) {
            cellHk.setBackground("#dcfce7").setFontColor("#15803d").setFontWeight("bold");
          } else {
            cellHk.setBackground("#fee2e2").setFontColor("#b91c1c").setFontWeight("bold");
          }
        }
        if (item.suy_hao !== undefined) {
          var valSh = String(item.suy_hao);
          var cellSh = sheet.getRange(targetRow, 12);
          cellSh.setValue(valSh);
          if (valSh.toLowerCase().indexOf("mất tín hiệu") !== -1) {
            cellSh.setBackground("#ffedd5").setFontColor("#c2410c").setFontWeight("bold");
          } else {
            cellSh.setBackground("#ffffff").setFontColor("#000000").setFontWeight("normal");
          }
        }
        updatedCount++;
      }
    }
    
    SpreadsheetApp.flush();
    return ContentService.createTextOutput(JSON.stringify({
      status: "success",
      updated: updatedCount,
      total_requested: updates.length
    })).setMimeType(ContentService.MimeType.JSON);
    
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      message: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}

function doGet(e) {
  return ContentService.createTextOutput(JSON.stringify({
    status: "online",
    message: "Google Apps Script Bypass Webhook đang hoạt động tốt!",
    timestamp: new Date().toISOString()
  })).setMimeType(ContentService.MimeType.JSON);
}
"""
