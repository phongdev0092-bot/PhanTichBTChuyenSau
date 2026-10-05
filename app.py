"""
app.py - MYBAE AUTO Dashboard - Flask Server
"""
import json
import threading
import time
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from config import FLASK_PORT, SECRET_KEY
from modules.auth import login, is_logged_in, close_driver, force_kill_driver, submit_manual_otp
from modules.sheet_reader import get_employees, get_contracts, get_team_captains
from modules.supabase_db import (
    fetch_table_summary, fetch_table_data, import_records,
    clear_table_data, update_supabase_key, get_supabase_key, extract_row_fields,
    extract_bao_tri_key, fetch_existing_bao_tri_keys, deduplicate_existing_bao_tri
)
from modules.analyzer import analyze_contract
from modules import inside_fpt
from modules import inside_import
from modules import branch_manager
from modules import captain_manager
import io
import pandas as pd




logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

import sys
import os

if getattr(sys, 'frozen', False):
    _base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        template_folder=os.path.join(_base_dir, 'templates'),
        static_folder=os.path.join(_base_dir, 'static')
    )
else:
    app = Flask(__name__)

app.secret_key = SECRET_KEY

# Global job state
_job_lock   = threading.Lock()
_job_results   = []
_job_progress  = 0
_job_total     = 0
_job_running   = False
_job_status    = "idle"   # idle | logging_in | running | done | error | cancelled
_job_message   = ""
_job_current_contract = ""
_job_step_msg  = ""
_job_step_num  = 0
_job_percent   = 0
_job_cancel_requested = False
_login_status  = "unknown"
_login_message = ""
_login_step    = ""


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


USER_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config.json")

def load_user_config():
    default_cfg = {
        "mode": "default",          # "default" | "custom"
        "email": "",
        "email_password": "",
        "inside_account": "",
        "inside_password": "",
        "use_inside_otp": True,
        "totp_secret": ""
    }
    if not os.path.exists(USER_CONFIG_FILE):
        return default_cfg
    try:
        with open(USER_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            default_cfg.update(data)
            return default_cfg
    except Exception as e:
        log.error(f"Lỗi đọc user_config.json: {e}")
        return default_cfg

def save_user_config(cfg_data):
    try:
        with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Lỗi ghi user_config.json: {e}")


@app.route("/api/user_config", methods=["GET", "POST"])
def api_user_config():
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = load_user_config()
        if "mode" in data: cfg["mode"] = data["mode"]
        if "email" in data: cfg["email"] = data["email"].strip()
        if "email_password" in data: cfg["email_password"] = data["email_password"].strip()
        if "inside_account" in data: cfg["inside_account"] = data["inside_account"].strip()
        if "inside_password" in data: cfg["inside_password"] = data["inside_password"].strip()
        if "use_inside_otp" in data: cfg["use_inside_otp"] = bool(data["use_inside_otp"])
        if "totp_secret" in data: cfg["totp_secret"] = data["totp_secret"].strip()
        save_user_config(cfg)
        return jsonify({"success": True, "config": cfg})
    else:
        return jsonify({"success": True, "config": load_user_config()})


@app.route("/api/login", methods=["POST"])
def api_login():
    global _login_status, _login_message, _login_step
    data = request.get_json() or {}
    mode = data.get("mode", "default")
    otp_method = data.get("otp_method", "auto")

    cfg = load_user_config()
    cfg["mode"] = mode
    if mode == "custom":
        if "email" in data: cfg["email"] = data.get("email", "").strip()
        if "email_password" in data: cfg["email_password"] = data.get("email_password", "").strip()
        if "inside_account" in data: cfg["inside_account"] = data.get("inside_account", "").strip()
        if "inside_password" in data: cfg["inside_password"] = data.get("inside_password", "").strip()
        if "use_inside_otp" in data: cfg["use_inside_otp"] = bool(data.get("use_inside_otp", True))
        if "totp_secret" in data: cfg["totp_secret"] = data.get("totp_secret", "").strip()

    save_user_config(cfg)

    # Nếu trạng thái login đã OK và mode không đổi, trả về OK luôn trừ khi bấm lại
    if _login_status == "ok" and not data.get("force_relogin"):
        return jsonify({"success": True, "message": "✅ Đã đăng nhập", "mode": mode, "status": "ok"})

    # Dọn dẹp driver cũ nếu force_relogin để không bị race condition hay zombie
    if data.get("force_relogin"):
        force_kill_driver()

    _login_status = "pending"
    _login_message = "Đang khởi tạo trình duyệt đăng nhập..."
    _login_step = "init"

    def do_login():
        global _login_status, _login_message, _login_step
        def step_cb(msg):
            global _login_message
            _login_message = msg
            log.info(f"Login Step: {msg}")

        try:
            if mode == "custom":
                ok, msg = login(
                    email_addr=cfg.get("email"),
                    email_pass=cfg.get("email_password"),
                    step_callback=step_cb,
                    otp_method=otp_method
                )
            else:
                ok, msg = login(step_callback=step_cb, otp_method=otp_method)

            if ok and msg == "waiting_otp":
                _login_status = "waiting_otp"
                _login_message = "Đã gửi mã OTP! Vui lòng nhập 6 chữ số OTP vào hệ thống."
            elif ok:
                _login_status = "ok"
                _login_message = msg
            else:
                _login_status = "error"
                _login_message = msg
        except Exception as e:
            _login_status = "error"
            _login_message = f"❌ Lỗi xử lý: {e}"
            force_kill_driver()
        finally:
            log.info(f"Login ({mode}) kết thúc với trạng thái {_login_status}: {_login_message}")

    t = threading.Thread(target=do_login, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "⏳ Đang bắt đầu đăng nhập...", "mode": mode, "status": "pending"})


@app.route("/api/login_status")
def api_login_status():
    cfg = load_user_config()
    return jsonify({
        "status": _login_status,
        "message": _login_message,
        "step": _login_step,
        "mode": cfg.get("mode", "default")
    })


@app.route("/api/login/submit_otp", methods=["POST"])
def api_login_submit_otp():
    """Endpoint nhận OTP thủ công do người dùng gõ vào form"""
    global _login_status, _login_message
    data = request.get_json() or {}
    otp = str(data.get("otp", "")).strip()
    if not otp or len(otp) != 6:
        return jsonify({"success": False, "error": "Mã OTP phải gồm 6 chữ số."})

    def step_cb(msg):
        global _login_message
        _login_message = msg

    _login_status = "pending"
    _login_message = f"Đang xác thực mã OTP ({otp})..."
    ok, msg = submit_manual_otp(otp, step_callback=step_cb)
    if ok:
        _login_status = "ok"
        _login_message = msg
        return jsonify({"success": True, "message": msg})
    else:
        _login_status = "error"
        _login_message = msg
        return jsonify({"success": False, "error": msg})


@app.route("/api/login/reset", methods=["POST"])
def api_login_reset():
    """Hủy tiến trình đăng nhập và dọn dẹp sạch sẽ tài nguyên trình duyệt."""
    global _login_status, _login_message
    force_kill_driver()
    _login_status = "idle"
    _login_message = "Đã dọn dẹp trình duyệt và đưa về trạng thái sẵn sàng."
    return jsonify({"success": True, "message": _login_message})


# ─────────────────────────────────────────────
#  BRANCH MANAGEMENT API ENDPOINTS
# ─────────────────────────────────────────────

@app.route("/api/branches", methods=["GET", "POST", "DELETE"])
def api_branches():
    """Quản lý danh sách Chi Nhánh."""
    try:
        if request.method == "GET":
            branches = branch_manager.load_branches()
            return jsonify({"success": True, "branches": branches})
        elif request.method == "POST":
            data = request.get_json() or {}
            name = str(data.get("name") or "").strip()
            ok, msg, branches = branch_manager.add_branch(name)
            return jsonify({"success": ok, "message": msg, "branches": branches}), (200 if ok else 400)
        elif request.method == "DELETE":
            data = request.get_json() or {}
            name = str(data.get("name") or "").strip()
            ok, msg, branches = branch_manager.delete_branch(name)
            return jsonify({"success": ok, "message": msg, "branches": branches}), (200 if ok else 400)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/employees")
def api_employees():
    """Trả về danh sách nhân viên từ đúng bảng (bao_tri / ton_bao_tri) và chi nhánh."""
    try:
        source = request.args.get("source", "bao_tri")
        branch = request.args.get("branch", "").strip() or None
        employees = get_employees(table=source, branch=branch)
        return jsonify({"success": True, "employees": employees, "source": source, "branch": branch})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/team_captains")
def api_team_captains():
    try:
        branch = request.args.get("branch", "").strip() or None
        force = request.args.get("force", "0") in ("1", "true", "True")
        data = get_team_captains(force=force, branch=branch)

        # Lấy thêm danh sách toàn bộ nhân viên có trong hệ thống (bao_tri + ton_bao_tri)
        emps_bt = get_employees(table="bao_tri", branch=branch) or []
        emps_ton = get_employees(table="ton_bao_tri", branch=branch) or []
        all_avail_emps = sorted(list(set(emps_bt).union(set(emps_ton))))

        captains_map = data.get("captains_map", {})
        all_mapped_emps = set()
        for emps in captains_map.values():
            all_mapped_emps.update(emps)

        unassigned_emps = [e for e in all_avail_emps if e not in all_mapped_emps]

        return jsonify({
            "success": True,
            "captains": data.get("sorted_captains", []),
            "captains_map": captains_map,
            "emp_to_captain": data.get("emp_to_captain", {}),
            "branch": branch,
            "total_captains": len(data.get("sorted_captains", [])),
            "total_mapped_employees": len(all_mapped_emps),
            "available_employees": all_avail_emps,
            "unassigned_employees": unassigned_emps
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/team_captains/save", methods=["POST"])
def api_team_captains_save():
    try:
        req = request.get_json() or {}
        captains_map = req.get("captains_map", {})
        branch = str(req.get("branch") or "").strip() or None

        ok, msg = captain_manager.save_captains(captains_map, branch=branch)
        if ok:
            get_team_captains(force=True, branch=branch)
            return jsonify({"success": True, "message": msg, "captains_map": captains_map})
        else:
            return jsonify({"success": False, "error": msg}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/team_captains/import", methods=["POST"])
def api_team_captains_import():
    try:
        branch = str(request.form.get("branch") or "").strip() or None
        if "file" not in request.files:
            return jsonify({"success": False, "error": "Vui lòng chọn file tải lên (.xlsx, .xls hoặc .csv)"}), 400

        file = request.files["file"]
        filename = (file.filename or "").lower()

        if filename.endswith(".csv"):
            df = pd.read_csv(file, header=0, dtype=str)
        elif filename.endswith(".xlsx") or filename.endswith(".xls"):
            df = pd.read_excel(file, header=0, dtype=str)
        else:
            return jsonify({"success": False, "error": "Chỉ hỗ trợ file Excel (.xlsx, .xls) hoặc .csv"}), 400

        ok, msg, cap_map = captain_manager.import_from_dataframe(df, branch=branch)
        if ok:
            get_team_captains(force=True, branch=branch)
            return jsonify({
                "success": True,
                "message": msg,
                "captains_map": cap_map,
                "captains": sorted(list(cap_map.keys()))
            })
        else:
            return jsonify({"success": False, "error": msg}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/team_captains/template")
def api_team_captains_template():
    try:
        excel_bytes = captain_manager.generate_template_excel()
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=Mau_MAP_Doi_Truong_Nhan_Vien.xlsx"}
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/team_captains/export")
def api_team_captains_export():
    try:
        branch = request.args.get("branch", "").strip() or None
        excel_bytes = captain_manager.export_excel(branch=branch)
        filename = f"Danh_Sach_MAP_Doi_Truong_{branch or 'Chung'}.xlsx"
        return Response(
            excel_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500




# ─────────────────────────────────────────────
#  SUPABASE DATABASE & IMPORT API ENDPOINTS
# ─────────────────────────────────────────────

@app.route("/api/supabase/tables_summary")
def api_supabase_tables_summary():
    try:
        branch = request.args.get("branch", "").strip() or None
        summary = fetch_table_summary(branch=branch)
        return jsonify({"success": True, "summary": summary, "current_key": get_supabase_key(), "branch": branch})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/supabase/update_key", methods=["POST"])
def api_supabase_update_key():
    try:
        data = request.get_json() or {}
        key = data.get("key", "").strip()
        if not key:
            return jsonify({"success": False, "error": "Khóa Supabase API Key không được rỗng"}), 400

        ok, msg = update_supabase_key(key)
        if ok:
            return jsonify({"success": True, "message": msg})
        else:
            return jsonify({"success": False, "error": msg}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/api/supabase/table_data")
def api_supabase_table_data():
    try:
        table_name = request.args.get("table", "contracts")
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        search = request.args.get("search", "").strip()
        branch = request.args.get("branch", "").strip() or None
        data = fetch_table_data(table_name=table_name, page=page, per_page=per_page, search=search, branch=branch)
        return jsonify({"success": True, "data": data, "branch": branch})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/supabase/import", methods=["POST"])
def api_supabase_import():
    try:
        req_json = request.get_json(silent=True) or {}
        table_name = request.form.get("table_name") or req_json.get("table_name")
        chi_nhanh = (
            request.form.get("chi_nhanh")
            or req_json.get("chi_nhanh")
            or request.args.get("chi_nhanh")
            or ""
        ).strip()

        if not table_name:
            return jsonify({"success": False, "error": "Thiếu tên bảng cần import"}), 400

        if not chi_nhanh:
            return jsonify({"success": False, "error": "Vui lòng chọn hoặc đặt tên Chi Nhánh trước khi import dữ liệu!"}), 400

        # Tự động thêm chi nhánh vào danh sách ghi nhớ nếu chưa có
        branch_manager.add_branch(chi_nhanh)

        records = []
        col_names = []

        # 1. Nếu upload qua File (CSV / Excel / JSON)
        if "file" in request.files:
            file = request.files["file"]
            filename = file.filename.lower()

            if filename.endswith(".csv"):
                df = pd.read_csv(file, header=0, dtype=str)
            elif filename.endswith(".xlsx") or filename.endswith(".xls"):
                df = pd.read_excel(file, header=0, dtype=str)
            elif filename.endswith(".json"):
                raw_json = json.load(file)
                df = None
                records = []
                for item in raw_json:
                    if isinstance(item, dict):
                        row_dict = item.get("data") if isinstance(item.get("data"), dict) else dict(item)
                        row_dict["chi_nhanh"] = chi_nhanh
                        row_dict["Chi nhánh"] = chi_nhanh
                        so_hd, nhan_vien, tg_hoan_tat, _ = extract_row_fields({"data": row_dict}, table=table_name)
                        records.append({
                            "so_hd": so_hd,
                            "nhan_vien": nhan_vien,
                            "tg_hoan_tat": tg_hoan_tat,
                            "data": row_dict
                        })
            else:
                return jsonify({"success": False, "error": "Định dạng file không hỗ trợ. Vui lòng chọn .csv, .xlsx, .xls hoặc .json"}), 400

            if df is not None:
                df.columns = [str(c).strip() for c in df.columns]
                col_names = df.columns.tolist()
                raw_rows = df.fillna("").to_dict(orient="records")

                records = []
                for row_dict in raw_rows:
                    so_hd, nhan_vien, tg_hoan_tat, _ = extract_row_fields({"data": row_dict}, table=table_name)
                    row_dict["chi_nhanh"] = chi_nhanh
                    row_dict["Chi nhánh"] = chi_nhanh
                    record = {
                        "so_hd": so_hd,
                        "nhan_vien": nhan_vien,
                        "tg_hoan_tat": tg_hoan_tat,
                        "data": row_dict
                    }
                    records.append(record)

        # 2. Nếu gửi qua JSON body trực tiếp
        elif request.is_json:
            json_data = request.get_json() or {}
            raw_records = json_data.get("records", [])
            records = []
            for item in raw_records:
                if isinstance(item, dict):
                    row_dict = item.get("data") if isinstance(item.get("data"), dict) else dict(item)
                    row_dict["chi_nhanh"] = chi_nhanh
                    row_dict["Chi nhánh"] = chi_nhanh
                    so_hd, nhan_vien, tg_hoan_tat, _ = extract_row_fields({"data": row_dict}, table=table_name)
                    records.append({
                        "so_hd": so_hd,
                        "nhan_vien": nhan_vien,
                        "tg_hoan_tat": tg_hoan_tat,
                        "data": row_dict
                    })

        if not records:
            return jsonify({"success": False, "error": "Không đọc được bản ghi nào từ file hoặc dữ liệu gửi lên."}), 400

        # TRƯỜNG HỢP 1: TỒN BẢO TRÌ (ton_bao_tri) -> XÓA SẠCH DỮ LIỆU CŨ CỦA CHI NHÁNH VÀ NẠP DỮ LIỆU MỚI
        if table_name == "ton_bao_tri":
            log.info(f"Bảng ton_bao_tri: Tiến hành xóa toàn bộ dữ liệu cũ của chi nhánh '{chi_nhanh}' trước khi nhập...")
            clear_ok, clear_msg = clear_table_data("ton_bao_tri", branch=chi_nhanh)
            if not clear_ok:
                log.warning(f"Cảnh báo khi làm sạch bảng ton_bao_tri chi nhánh '{chi_nhanh}': {clear_msg}")

            ok, msg, inserted_count = import_records("ton_bao_tri", records)
            if ok:
                msg_custom = f"✅ Đã làm mới thành công {inserted_count} bản ghi Tồn Bảo Trì cho Chi Nhánh '{chi_nhanh}' (Dữ liệu cũ đã được xóa sạch theo quy tắc ca tồn)."
                return jsonify({"success": True, "message": msg_custom, "count": inserted_count, "chi_nhanh": chi_nhanh})
            else:
                return jsonify({"success": False, "error": msg}), 500

        # TRƯỜNG HỢP 2: BẢO TRÌ (bao_tri) -> GIỮ NGUYÊN DATA HIỆN CÓ, LỌC TRÙNG 5 CỘT (A, E, F, D, AR), CHỈ NẠP/UPDATE DATA MỚI
        elif table_name == "bao_tri":
            log.info(f"Bảng bao_tri: Giữ nguyên dữ liệu hiện có, kích hoạt cơ chế lọc trùng 5 cột (A: Số HĐ, E: TG Tạo, F: TG Hoàn Tất, D: Nhân viên, AR: Account Tạo CL)...")

            # Bước 2.1: Lọc trùng lặp ngay trong chính file tải lên (giữ bản ghi cuối cùng trong file)
            file_unique_records = []
            seen_file_keys = set()
            file_dupes = 0

            for rec in records:
                k = extract_bao_tri_key(rec, col_names=col_names)
                if not k[0]:  # Không có Số HĐ
                    continue
                if k in seen_file_keys:
                    file_dupes += 1
                    continue
                seen_file_keys.add(k)
                file_unique_records.append(rec)

            if not file_unique_records:
                return jsonify({"success": False, "error": "Không tìm thấy bản ghi hợp lệ có Số HĐ trong file tải lên."}), 400

            # Bước 2.2: Lấy danh sách Số HĐ từ file để truy vấn các bản ghi đối ứng hiện có trên Supabase
            incoming_hds = [r.get("so_hd") for r in file_unique_records if r.get("so_hd")]
            existing_map = fetch_existing_bao_tri_keys(incoming_hds, branch=chi_nhanh)
            log.info(f"Supabase bao_tri: Tìm thấy {len(existing_map)} keys đã tồn tại khớp với các Số HĐ trong file")

            # Bước 2.3: Phân loại bản ghi thành THÊM MỚI (Insert) và TRÙNG / CẬP NHẬT (Update)
            records_to_insert = []
            records_to_update = []

            for rec in file_unique_records:
                k = extract_bao_tri_key(rec, col_names=col_names)
                if k in existing_map:
                    # Trùng khớp hoàn toàn cả 5 cột (A Số HĐ, E TG Tạo, F TG Hoàn Tất, D Nhân viên, AR Account Tạo CL)
                    # Gán id đã có để Supabase update đè dữ liệu mới nhất, KHÔNG sinh bản ghi trùng lặp
                    rec_copy = dict(rec)
                    rec_copy["id"] = existing_map[k]
                    records_to_update.append(rec_copy)
                else:
                    # Bản ghi mới chưa từng có trên hệ thống
                    rec_copy = dict(rec)
                    rec_copy.pop("id", None)
                    records_to_insert.append(rec_copy)

            total_inserted = 0
            total_updated = 0

            # Ghi bản ghi mới vào Supabase
            if records_to_insert:
                ok_ins, msg_ins, count_ins = import_records("bao_tri", records_to_insert)
                if not ok_ins:
                    return jsonify({"success": False, "error": f"Lỗi khi thêm mới bản ghi Bảo Trì: {msg_ins}"}), 500
                total_inserted = count_ins

            # Cập nhật bản ghi trùng lặp (nếu có)
            if records_to_update:
                ok_upd, msg_upd, count_upd = import_records("bao_tri", records_to_update)
                if not ok_upd:
                    log.warning(f"Lỗi khi cập nhật bản ghi trùng lặp Bảo Trì: {msg_upd}")
                else:
                    total_updated = count_upd

            msg_parts = [
                f"✅ Import Bảo Trì thành công cho Chi Nhánh '{chi_nhanh}':",
                f"• Thêm mới: {total_inserted} bản ghi.",
                f"• Cập nhật / Đã lọc trùng: {len(records_to_update)} bản ghi (khớp 5 cột A Số HĐ, E TG Tạo, F TG Hoàn Tất, D Nhân viên, AR Account Tạo CL).",
                f"• Giữ nguyên vẹn toàn bộ dữ liệu lịch sử đã có trong hệ thống."
            ]
            if file_dupes > 0:
                msg_parts.append(f"• (Đã loại bỏ {file_dupes} dòng trùng nhau ngay trong file tải lên).")

            return jsonify({
                "success": True,
                "message": "\n".join(msg_parts),
                "inserted_count": total_inserted,
                "updated_count": total_updated,
                "chi_nhanh": chi_nhanh
            })

        # TRƯỜNG HỢP CÁC BẢNG KHÁC (employees, cll30, kh_cls...)
        else:
            ok, msg, inserted_count = import_records(table_name, records)
            if ok:
                return jsonify({"success": True, "message": f"✅ Đã import {inserted_count} bản ghi vào bảng '{table_name}'", "count": inserted_count, "chi_nhanh": chi_nhanh})
            else:
                return jsonify({"success": False, "error": msg}), 500

    except Exception as e:
        log.error(f"Lỗi API import: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/supabase/clear_table", methods=["POST"])
def api_supabase_clear_table():
    try:
        data = request.get_json() or {}
        table_name = data.get("table_name")
        branch = (data.get("chi_nhanh") or data.get("branch") or "").strip() or None
        if not table_name:
            return jsonify({"success": False, "error": "Thiếu tên bảng cần xóa"}), 400

        ok, msg = clear_table_data(table_name, branch=branch)
        if ok:
            return jsonify({"success": True, "message": msg, "branch": branch})
        else:
            return jsonify({"success": False, "error": msg}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/supabase/deduplicate", methods=["POST"])
def api_supabase_deduplicate():
    try:
        data = request.get_json() or {}
        branch = (data.get("chi_nhanh") or data.get("branch") or "").strip() or None
        ok, msg, count = deduplicate_existing_bao_tri(branch=branch)
        if ok:
            return jsonify({"success": True, "message": msg, "count": count, "branch": branch})
        else:
            return jsonify({"success": False, "error": msg}), 500
    except Exception as e:
        log.error(f"Lỗi API deduplicate: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



# ─────────────────────────────────────────────
#  IMPORT TỰ ĐỘNG TỒN BẢO TRÌ TỪ INSIDE - API ENDPOINTS
# ─────────────────────────────────────────────

def _read_inside_export(path):
    """Đọc file Excel xuất từ Inside (tự dò dòng tiêu đề, hỗ trợ xls HTML)."""
    raw = None
    try:
        raw = pd.read_excel(path, header=None, dtype=str)
    except Exception:
        try:
            raw = pd.read_html(path, header=None)[0].astype(str)
        except Exception:
            raw = pd.read_csv(path, header=None, dtype=str)

    header_idx = 0
    for i in range(min(15, len(raw))):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        if any(c in ("số hđ", "số hd", "số hợp đồng", "stt") for c in cells):
            header_idx = i
            break
    df = raw.iloc[header_idx + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_idx].tolist()]
    return df.reset_index(drop=True)


def _import_ton_bao_tri_file(path, chi_nhanh):
    """Import file Excel Inside vào ton_bao_tri (xóa dữ liệu cũ của chi nhánh rồi nạp mới)."""
    df = _read_inside_export(path)
    raw_rows = df.fillna("").to_dict(orient="records")
    records = []
    for row_dict in raw_rows:
        if not any(str(v).strip() for v in row_dict.values()):
            continue
        so_hd, nhan_vien, tg_hoan_tat, _ = extract_row_fields({"data": row_dict}, table="ton_bao_tri")
        if not so_hd:
            continue
        row_dict["chi_nhanh"] = chi_nhanh
        row_dict["Chi nhánh"] = chi_nhanh
        records.append({"so_hd": so_hd, "nhan_vien": nhan_vien, "tg_hoan_tat": tg_hoan_tat, "data": row_dict})

    if not records:
        return False, "File Excel không có bản ghi hợp lệ (không đọc được Số HĐ)", 0

    branch_manager.add_branch(chi_nhanh)
    clear_ok, clear_msg = clear_table_data("ton_bao_tri", branch=chi_nhanh)
    if not clear_ok:
        log.warning(f"Cảnh báo khi làm sạch ton_bao_tri chi nhánh '{chi_nhanh}': {clear_msg}")
    ok, msg, inserted = import_records("ton_bao_tri", records)
    if not ok:
        return False, msg, 0
    return True, f"✅ Đã import tự động {inserted} bản ghi Tồn Bảo Trì cho Chi Nhánh '{chi_nhanh}'.", inserted


@app.route("/api/auto_import/start", methods=["POST"])
def api_auto_import_start():
    """Bắt đầu import tự động Tồn Bảo Trì từ Inside."""
    if inside_import._running or inside_fpt._auto_note_running:
        return jsonify({"success": False, "error": "Đang có tiến trình Inside khác chạy (Auto Import / Auto Note), vui lòng đợi."}), 400

    data = request.get_json() or {}
    chi_nhanh = str(data.get("chi_nhanh", "")).strip()
    if not chi_nhanh:
        return jsonify({"success": False, "error": "Vui lòng chọn Chi Nhánh trước khi import tự động!"}), 400

    cfg = load_user_config()
    totp_secret = str(data.get("totp_secret", "")).strip() or cfg.get("totp_secret") or None

    def on_file(path):
        return _import_ton_bao_tri_file(path, chi_nhanh)

    def run_job():
        if cfg.get("mode") == "custom":
            inside_import.run_auto_import(
                on_file,
                account=cfg.get("inside_account"),
                password=cfg.get("inside_password"),
                use_otp=cfg.get("use_inside_otp", True),
                totp_secret=totp_secret,
            )
        else:
            inside_import.run_auto_import(on_file, totp_secret=totp_secret)

    threading.Thread(target=run_job, daemon=True).start()
    d_from, d_to = inside_import.get_date_range()
    return jsonify({
        "success": True,
        "message": f"Bắt đầu import tự động Tồn Bảo Trì ({d_from:%d/%m/%Y} → {d_to:%d/%m/%Y})...",
    })


@app.route("/api/auto_import/status")
def api_auto_import_status():
    return jsonify({"success": True, "state": inside_import.get_state()})


@app.route("/api/auto_import/cancel", methods=["POST"])
def api_auto_import_cancel():
    inside_import.cancel()
    return jsonify({"success": True, "message": "Đã gửi lệnh dừng import tự động"})


# ─────────────────────────────────────────────
#  AUTO NOTE TON BAO TRI - API ENDPOINTS
# ─────────────────────────────────────────────

@app.route("/api/auto_note/start", methods=["POST"])
def api_auto_note_start():
    """Bắt đầu job tự động ghi chú vào FPT Inside."""
    if inside_fpt._auto_note_running:
        return jsonify({"success": False, "error": "Job đang chạy, vui lòng đợi hoặc dừng trước"}), 400

    data = request.get_json() or {}
    results_list = data.get("results", [])

    cfg = load_user_config()
    totp_secret = data.get("totp_secret", "").strip() or cfg.get("totp_secret") or None

    if not results_list:
        return jsonify({"success": False, "error": "Không có hợp đồng nào được chọn để ghi chú"}), 400

    # Lấy danh sách HĐ đã note thành công trong 24h qua
    recently_noted = inside_fpt.get_successful_noted_contracts_24h(hours=24)

    # Chỉ lấy những hợp đồng có cảnh báo hoặc cần xử lý VÀ chưa note thành công trong 24H qua
    to_note = [
        r for r in results_list
        if ((r.get("canh_bao") and r.get("canh_bao") != "—")
            or (r.get("can_xu_ly") and r.get("can_xu_ly") != "—"))
        and ((r.get("so_hd") or "").strip().upper() not in recently_noted)
    ]

    if not to_note:
        return jsonify({"success": False, "error": "Không có hợp đồng nào đủ điều kiện ghi chú (các HĐ có cảnh báo đã được note auto thành công trong 24H qua)"}), 400

    def run_job():
        if cfg.get("mode") == "custom":
            inside_fpt.run_auto_note(
                to_note,
                account=cfg.get("inside_account"),
                password=cfg.get("inside_password"),
                use_otp=cfg.get("use_inside_otp", True),
                totp_secret=totp_secret
            )
        else:
            inside_fpt.run_auto_note(to_note, totp_secret=totp_secret)

    t = threading.Thread(target=run_job, daemon=True)
    t.start()

    return jsonify({
        "success": True,
        "total":   len(to_note),
        "message": f"Bắt đầu Auto Note {len(to_note)} hợp đồng..."
    })


@app.route("/api/auto_note/status")
def api_auto_note_status():
    """Trả về trạng thái hiện tại của job Auto Note."""
    state = inside_fpt.get_auto_note_state()
    return jsonify({"success": True, "state": state})


@app.route("/api/auto_note/cancel", methods=["POST"])
def api_auto_note_cancel():
    inside_fpt.cancel_auto_note()
    return jsonify({"success": True, "message": "Da gui lenh dung Auto Note"})


@app.route("/api/auto_note/test_totp")
def api_test_totp():
    """Kiểm tra TOTP secret hiện tại – generate mã OTP ngậy bây giờ."""
    try:
        import config
        import pyotp, time as _time
        secret = getattr(config, "TOTP_SECRET", "").strip()
        if not secret:
            return jsonify({"success": False, "error": "Chưa cấu hình TOTP Secret"})
        totp  = pyotp.TOTP(secret)
        code  = totp.now()
        remaining = 30 - int(_time.time()) % 30
        return jsonify({"success": True, "otp": code, "remaining_seconds": remaining,
                        "account": "Phuongnam.phongnh5@inside.fpt.net"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/auto_note/decode_qr", methods=["POST"])
def api_decode_qr():
    """Nhận file ảnh QR, giải mã và lưu TOTP secret vào config."""
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "Không có file QR gửi lên"}), 400
        file = request.files["file"]
        import tempfile, os
        suffix = os.path.splitext(file.filename)[-1] or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        secret = inside_fpt.decode_qr_secret(tmp_path)
        os.unlink(tmp_path)

        if secret:
            inside_fpt.save_totp_secret(secret)
            import pyotp
            otp = pyotp.TOTP(secret).now()
            return jsonify({"success": True, "secret": secret, "otp_sample": otp,
                            "message": "Da giai ma QR va luu TOTP Secret thanh cong!"})
        else:
            return jsonify({"success": False, "error": "Khong doc duoc QR code tu anh nay"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/cancel", methods=["POST"])


def api_cancel():
    global _job_cancel_requested, _job_status, _job_message
    if not _job_running:
        return jsonify({"success": False, "message": "Không có tiến trình nào đang chạy"})
    _job_cancel_requested = True
    _job_message = "Đang dừng tiến trình..."
    return jsonify({"success": True, "message": "🛑 Đã gửi lệnh ngừng tiến trình"})


import os

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis_history.json")

from modules import zstore

def load_history():
    return zstore.load_json(HISTORY_FILE, [])

def get_analyzed_contracts_24h(hours: float = 24.0) -> set[str]:
    """
    Trả về tập hợp các mã HĐ (so_hd) đã được phân tích thành công trong vòng `hours` giờ qua.
    Mã HĐ được chuẩn hóa viết hoa & xén khoảng trắng (upper & strip).
    """
    history = load_history()
    analyzed_contracts = set()
    now = time.time()
    cutoff_time = now - (hours * 3600)

    for run in history:
        ts_str = run.get("timestamp", "")
        if not ts_str:
            continue
        try:
            run_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            run_ts = run_dt.timestamp()
        except Exception:
            try:
                run_dt = datetime.fromisoformat(ts_str)
                run_ts = run_dt.timestamp()
            except Exception:
                continue

        if run_ts >= cutoff_time:
            for item in run.get("results", []):
                if item.get("status") != "error":
                    so_hd = (item.get("so_hd") or "").strip().upper()
                    if so_hd:
                        analyzed_contracts.add(so_hd)

    return analyzed_contracts

def save_history_run(run_data):
    history = load_history()
    history.insert(0, run_data)
    try:
        zstore.save_json(HISTORY_FILE, history)
    except Exception as e:
        log.error(f"Error saving history: {e}")


@app.route("/api/filter", methods=["POST"])
def api_filter():
    global _job_results, _job_progress, _job_total, _job_running, _job_status, _job_message, _job_cancel_requested

    data = request.get_json() or {}
    selected = data.get("employees", [])
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    team_captain = str(data.get("team_captain") or "").strip()
    branch = str(data.get("chi_nhanh") or data.get("branch") or "").strip() or None
    data_source = data.get("data_source", "bao_tri")  # bao_tri | ton_bao_tri
    if data_source not in ("bao_tri", "ton_bao_tri"):
        data_source = "bao_tri"
    if not team_captain:
        team_captain = None

    # Xử lý ghép danh sách nhân viên:
    final_selected = set(selected)
    
    # Nếu chọn Đội Trưởng, gom nhân viên thuộc Đội Trưởng
    if team_captain:
        captains_data = get_team_captains(branch=branch)
        cap_map = captains_data.get("captains_map", {})
        captain_members = cap_map.get(team_captain)
        if not captain_members:
            for k, v in cap_map.items():
                if k.strip().lower() == team_captain.strip().lower():
                    captain_members = v
                    break
        if captain_members:
            final_selected.update(captain_members)

    # Lấy danh sách hợp đồng từ đúng bảng và chi nhánh
    try:
        contracts = get_contracts(list(final_selected), start_date=start_date,
                                  end_date=end_date, table=data_source, branch=branch)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    # Nếu là Phân Tích Tồn (ton_bao_tri): Lọc bỏ các HĐ ĐÃ PHÂN TÍCH hoặc ĐÃ NOTE AUTO thành công trong 24H qua
    skipped_24h_count = 0
    if data_source == "ton_bao_tri":
        try:
            recently_noted = inside_fpt.get_successful_noted_contracts_24h(hours=24)
            recently_analyzed = get_analyzed_contracts_24h(hours=24)
            skip_set_24h = recently_noted.union(recently_analyzed)

            if skip_set_24h:
                orig_len = len(contracts)
                contracts = [
                    c for c in contracts
                    if (c.get("so_hd") or "").strip().upper() not in skip_set_24h
                ]
                skipped_24h_count = orig_len - len(contracts)
                if skipped_24h_count > 0:
                    log.info(f"[Tồn Bảo Trì] Đã bỏ qua {skipped_24h_count}/{orig_len} HĐ do đã phân tích hoặc note auto thành công trong 24H qua.")
        except Exception as e_skip:
            log.error(f"Lỗi khi lọc HĐ tồn 24h: {e_skip}")

    if not contracts:
        msg = "Không tìm thấy hợp đồng nào phù hợp trong khoảng thời gian đã chọn."
        if skipped_24h_count > 0:
            msg = f"Tất cả {skipped_24h_count} HĐ Tồn Bảo Trì đã được phân tích hoặc note auto thành công trong 24H qua nên được bỏ qua không chạy lại."
        return jsonify({
            "success": True,
            "total": 0,
            "contracts": [],
            "skipped_24h_count": skipped_24h_count,
            "message": msg
        })

    # Đảm bảo danh sách hợp đồng duy nhất 100% không bị trùng lặp
    unique_contracts = []
    seen_hds = set()
    for c in contracts:
        hd_clean = (c.get("so_hd") or "").strip().upper()
        if hd_clean and hd_clean not in seen_hds:
            seen_hds.add(hd_clean)
            unique_contracts.append(c)
    contracts = unique_contracts

    analysis_mode = data.get("analysis_mode", "full")  # "full" | "compact"

    # Nếu chỉ kiểm tra số lượng HĐ (dùng để hiện popup hỏi chế độ phân tích nếu >= 10 HĐ)
    if data.get("check_count_only"):
        return jsonify({
            "success": True,
            "total": len(contracts),
            "skipped_24h_count": skipped_24h_count,
            "message": f"Tìm thấy {len(contracts)} hợp đồng phù hợp."
        })

    # Ngăn chặn tuyệt đối việc chạy đè nhiều luồng cùng lúc (gây quét trùng lặp và xung đột trình duyệt)
    with _job_lock:
        if _job_running:
            return jsonify({
                "success": False,
                "error": "Một tiến trình phân tích đang được thực thi. Vui lòng đợi hoàn tất hoặc bấm Hủy tiến trình trước khi chạy phiên mới!"
            }), 400

        # Reset cờ ngắt tiến trình
        _job_cancel_requested = False

        # Bắt đầu job chạy nền
        _job_results          = []
        _job_progress         = 0
        _job_total            = len(contracts)
        _job_running          = True
        _job_status           = "running"
        _job_message          = f"Đang phân tích {len(contracts)} hợp đồng ({'Rút gọn' if analysis_mode == 'compact' else 'Đầy đủ'})..."
        _job_current_contract = ""
        _job_step_msg         = "Đang khởi tạo tiến trình phân tích..."
        _job_step_num         = 0
        _job_percent          = 0

    def run_job():
        global _job_results, _job_progress, _job_running, _job_status, _job_message
        global _job_current_contract, _job_step_msg, _job_step_num, _job_percent
        total_cnt = len(contracts)
        try:
            for i, c in enumerate(contracts):
                # Kiểm tra yêu cầu hủy tiến trình từ người dùng
                if _job_cancel_requested:
                    log.info("Tiến trình đã bị ngắt bởi người dùng.")
                    with _job_lock:
                        _job_running = False
                        _job_status  = "cancelled"
                        _job_message = f"Đã dừng tiến trình tại HĐ {i}/{total_cnt}"
                        _job_step_msg = "Tiến trình đã bị người dùng dừng"
                    break

                contract_code = c["so_hd"]
                log.info(f"[{i+1}/{total_cnt}] Phân tích HĐ: {contract_code} (Chế độ: {analysis_mode})")

                # Callback cập nhật từng bước nhỏ & % tiến trình live
                def make_step_cb(idx, code):
                    def cb(step_msg, step_num):
                        global _job_current_contract, _job_step_msg, _job_step_num, _job_percent, _job_message
                        with _job_lock:
                            _job_current_contract = code
                            _job_step_msg = step_msg
                            _job_step_num = step_num
                            step_progress = (step_num - 1) / 6.0
                            pct = int(((idx + step_progress) / total_cnt) * 100)
                            _job_percent = min(99, max(0, pct))
                            _job_message = f"Đang chẩn đoán HĐ [{code}] ({idx+1}/{total_cnt})"
                    return cb

                step_cb = make_step_cb(i, contract_code)

                try:
                    res = analyze_contract(
                        contract_code,
                        c.get("tg_hoan_tat", ""),
                        step_callback=step_cb,
                        data_source=c.get("data_source", data_source),
                        analysis_mode=analysis_mode
                    )
                except Exception as exc:
                    log.exception(f"Lỗi khi phân tích HĐ {contract_code}")
                    res = {
                        "so_hd": contract_code,
                        "nhan_vien": c["nhan_vien"],
                        "tg_hoan_tat": c["tg_hoan_tat"],
                        "ngay": c["ngay"],
                        "xu_ly_loi_tu_dong": "",
                        "can_xu_ly": "",
                        "canh_bao": "",
                        "cong_suat_thu": "",
                        "so_lan_rot": "",
                        "loai_modem": "",
                        "mo_hinh_mang": "",
                        "phien_ban_pm": "",
                        "dns_wan": "",
                        "doi_chieu_rot_mang": "",
                        "doi_chieu_tap_diem": "",
                        "phan_tich_client": "—",
                        "status": "error",
                        "error": str(exc),
                    }

                # Gộp thông tin sheet vào kết quả
                res.setdefault("nhan_vien", c["nhan_vien"])
                res.setdefault("tg_hoan_tat", c["tg_hoan_tat"])
                res.setdefault("ngay", c["ngay"])
                res["nhan_vien"]   = c["nhan_vien"]
                res["tg_hoan_tat"] = c["tg_hoan_tat"]
                res["ngay"]        = c["ngay"]

                with _job_lock:
                    _job_results.append(res)
                    _job_progress = i + 1
                    _job_percent = int(((i + 1) / total_cnt) * 100)

            if not _job_cancel_requested:
                with _job_lock:
                    _job_running = False
                    _job_status  = "done"
                    _job_percent = 100
                    _job_step_msg = "Hoàn tất phân tích tất cả hợp đồng"
                    _job_message = f"Hoàn tất {total_cnt} hợp đồng"

            # Lưu vào lịch sử phân tích
            try:
                run_record = {
                    "id": f"run_{int(time.time()*1000)}",
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "start_date": start_date or "",
                    "end_date": end_date or "",
                    "team_captain": team_captain or "",
                    "chi_nhanh": branch or "",
                    "data_source": data_source,
                    "analysis_mode": analysis_mode,
                    "employees": list(final_selected),
                    "total_contracts": len(_job_results),
                    "auto_count": sum(1 for r in _job_results if r.get("xu_ly_loi_tu_dong") and r.get("xu_ly_loi_tu_dong") != "—"),
                    "need_count": sum(1 for r in _job_results if r.get("can_xu_ly") and r.get("can_xu_ly") != "—"),
                    "warn_count": sum(1 for r in _job_results if r.get("canh_bao") and r.get("canh_bao") != "—"),
                    "error_count": sum(1 for r in _job_results if r.get("status") == "error"),
                    "results": list(_job_results),
                }
                save_history_run(run_record)
            except Exception as hist_err:
                log.error(f"Lỗi khi lưu lịch sử: {hist_err}")

        except Exception as exc:
            log.exception("run_job có lỗi tổng quát")
            with _job_lock:
                _job_running = False
                _job_status  = "error"
                _job_message = f"Lỗi phân tích: {exc}"

    t = threading.Thread(target=run_job, daemon=True)
    t.start()

    start_msg = f"Bắt đầu phân tích {len(contracts)} hợp đồng"
    if skipped_24h_count > 0:
        start_msg += f" (Đã bỏ qua {skipped_24h_count} HĐ do đã phân tích hoặc note auto trong 24H)"

    return jsonify({
        "success":  True,
        "total":    len(contracts),
        "skipped_24h_count": skipped_24h_count,
        "message":  start_msg,
    })



@app.route("/api/history", methods=["GET"])
def api_get_history():
    return jsonify({"success": True, "history": load_history()})


@app.route("/api/history", methods=["DELETE"])
def api_clear_history():
    try:
        zstore.remove(HISTORY_FILE)
        return jsonify({"success": True, "message": "Đã xóa lịch sử"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/note_history", methods=["GET"])
def api_get_note_history():
    return jsonify({"success": True, "history": inside_fpt.load_note_history()})


@app.route("/api/note_history", methods=["DELETE"])
def api_clear_note_history():
    try:
        inside_fpt.clear_note_history()
        return jsonify({"success": True, "message": "Đã xóa lịch sử note"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/api/analytics")
def api_analytics():
    import re
    branch_filter = request.args.get("branch", "").strip()
    history = load_history()

    # Thu thập danh sách chi nhánh có trong hệ thống và lịch sử
    available_branches = list(branch_manager.load_branches())
    for run in history:
        b_name = (run.get("chi_nhanh") or "").strip()
        if b_name and b_name not in available_branches:
            available_branches.append(b_name)

    filtered_results = []
    total_runs = 0
    for run in history:
        run_branch = (run.get("chi_nhanh") or "").strip()
        run_res = run.get("results", [])
        
        # Nếu có chọn chi nhánh thì lọc
        if branch_filter:
            matched_items = []
            for r in run_res:
                item_branch = (r.get("chi_nhanh") or run_branch or "Sài Gòn 1").strip()
                if item_branch.lower() == branch_filter.lower():
                    matched_items.append((r, item_branch))
            if matched_items:
                total_runs += 1
                filtered_results.extend(matched_items)
        else:
            total_runs += 1
            for r in run_res:
                item_branch = (r.get("chi_nhanh") or run_branch or "Sài Gòn 1").strip()
                filtered_results.append((r, item_branch))

    total_contracts_analyzed = len(filtered_results)

    # 4 Tiêu chí thống kê cốt lõi sau phục vụ
    # 1. Sóng Wifi yếu (Thu phát kém)
    # 2. Suy hao không đạt chuẩn (Công suất thu < -23.5dBm)
    # 3. Các lần kết nối không ổn định, Rớt kết nối liên tục
    # 4. Thiết bị chính sử dụng lâu ngày chưa tắt mở
    crit_wifi_count = 0
    crit_suy_hao_count = 0
    crit_rot_count = 0
    crit_uptime_count = 0

    emp_map = {}

    for r, b_name in filtered_results:
        nv = (r.get("nhan_vien") or "KXD").strip()
        key = (b_name, nv)
        if key not in emp_map:
            emp_map[key] = {
                "branch": b_name,
                "nhan_vien": nv,
                "total_contracts": 0,
                "c1_wifi": 0,
                "c2_suy_hao": 0,
                "c3_rot": 0,
                "c4_uptime": 0,
                "contracts_with_warn": 0,
            }

        e = emp_map[key]
        e["total_contracts"] += 1

        cb = str(r.get("canh_bao") or "")
        cxl = str(r.get("can_xu_ly") or "")
        rot_str = str(r.get("doi_chieu_rot_mang") or "")
        client_str = str(r.get("phan_tich_client") or "")
        pwr_str = str(r.get("cong_suat_thu") or "")
        so_rot = str(r.get("so_lan_rot") or "")

        all_text = f"{cb} | {cxl} | {rot_str} | {client_str}".lower()

        # 1. Sóng Wifi yếu (Thu phát kém)
        c1 = (
            "sóng wifi yếu" in all_text or
            "thu phát kém" in all_text or
            "thiết bị kém" in client_str.lower() or
            "tbi wf kém" in client_str.lower() or
            "bắt kém" in client_str.lower() or
            "wifi kém" in all_text or
            "wifi yếu" in all_text
        )

        # 2. Suy hao không đạt chuẩn (Công suất thu < -23.5dBm)
        c2 = False
        if "suy hao không đạt chuẩn" in all_text or "vượt ngưỡng -23.5" in all_text or "suy hao cao" in all_text:
            c2 = True
        else:
            m_p = re.search(r'[-+]?\d+\.?\d*', pwr_str)
            if m_p:
                try:
                    v = float(m_p.group(0))
                    vp = -abs(v) if abs(v) >= 0.001 else 0.0
                    if vp < -23.5:
                        c2 = True
                except Exception:
                    pass

        # 3. Các lần kết nối không ổn định, Rớt kết nối liên tục
        c3 = False
        if (
            "các lần kết nối không ổn định" in all_text or
            "rớt kết nối liên tục" in all_text or
            "rớt liên tục" in all_text or
            "lịch sử kết nối rớt liên tục" in all_text
        ):
            c3 = True
        else:
            m_r = re.search(r'\d+', so_rot)
            if m_r and int(m_r.group(0)) >= 2:
                c3 = True
            elif "ra mạng" in rot_str.lower() and "ngày" in rot_str.lower():
                c3 = True

        # 4. Thiết bị chính sử dụng lâu ngày chưa tắt mở
        c4 = (
            "lâu ngày chưa tắt mở" in all_text or
            "chưa tắt mở" in all_text or
            "thiết bị chính sử dụng lâu ngày" in all_text or
            "tắt mở thiết bị thường xuyên" in all_text or
            "khởi động lại thiết bị" in all_text
        )

        if c1:
            crit_wifi_count += 1
            e["c1_wifi"] += 1
        if c2:
            crit_suy_hao_count += 1
            e["c2_suy_hao"] += 1
        if c3:
            crit_rot_count += 1
            e["c3_rot"] += 1
        if c4:
            crit_uptime_count += 1
            e["c4_uptime"] += 1

        if c1 or c2 or c3 or c4:
            e["contracts_with_warn"] += 1

    # Đánh giá nhân sự theo số lần cảnh báo / tổng HĐ chạy
    assessment_list = []
    total_violations_all = 0
    total_warn_contracts_all = 0

    for e in emp_map.values():
        tot = e["total_contracts"]
        tot_warn = e["c1_wifi"] + e["c2_suy_hao"] + e["c3_rot"] + e["c4_uptime"]
        warn_contracts = e["contracts_with_warn"]
        
        total_violations_all += tot_warn
        total_warn_contracts_all += warn_contracts

        rate = round((warn_contracts / tot) * 100, 1) if tot > 0 else 0.0
        e["total_violations"] = tot_warn
        e["violation_rate"] = rate

        # Xác định tiêu chí vi phạm nhiều nhất
        top_crit = []
        if e["c1_wifi"] > 0: top_crit.append(("Sóng Wifi yếu", e["c1_wifi"]))
        if e["c2_suy_hao"] > 0: top_crit.append(("Suy hao cao", e["c2_suy_hao"]))
        if e["c3_rot"] > 0: top_crit.append(("Rớt kết nối", e["c3_rot"]))
        if e["c4_uptime"] > 0: top_crit.append(("Chưa tắt mở modem", e["c4_uptime"]))
        top_crit.sort(key=lambda x: x[1], reverse=True)
        top_str = f"Chủ yếu: {top_crit[0][0]} ({top_crit[0][1]} HĐ)" if top_crit else "Không có lỗi"

        # Kết luận đánh giá nhân sự
        if tot_warn == 0:
            rating = "Xuất sắc"
            level = "tot"
            concl = "Xuất sắc: 100% đạt chuẩn sau phục vụ, 0 cảnh báo"
        elif rate <= 20.0:
            rating = "Đạt yêu cầu"
            level = "dat"
            concl = f"Đạt chuẩn: Tỷ lệ cảnh báo thấp ({rate}%). {top_str}"
        elif rate <= 40.0:
            rating = "Cần lưu ý"
            level = "luu_y"
            concl = f"Cần lưu ý: Tỷ lệ cảnh báo {rate}%. {top_str}"
        else:
            rating = "Cảnh báo cao"
            level = "canh_bao"
            concl = f"Cảnh báo cao: {warn_contracts}/{tot} HĐ vi phạm ({rate}%, {tot_warn} lỗi). {top_str}. Cần chấn chỉnh & tái đào tạo"

        e["rating"] = rating
        e["level"] = level
        e["conclusion"] = concl
        e["top_violation"] = top_str

        # Backward compatibility fields
        e["need_count"] = e["c2_suy_hao"] + e["c3_rot"]
        e["warn_count"] = e["c1_wifi"] + e["c4_uptime"]
        e["suy_hao_count"] = e["c2_suy_hao"]
        e["rot_count"] = e["c3_rot"]
        e["auto_count"] = 0

        assessment_list.append(e)

    # Sắp xếp nhân viên vi phạm nhiều nhất lên đầu
    assessment_list.sort(key=lambda x: (x["total_violations"], x["violation_rate"]), reverse=True)

    pass_contracts_all = max(0, total_contracts_analyzed - total_warn_contracts_all)
    overall_pass_rate = round((pass_contracts_all / total_contracts_analyzed) * 100, 1) if total_contracts_analyzed > 0 else 100.0

    criteria_summary = {
        "c1_wifi": {
            "label": "Sóng Wifi yếu (Thu phát kém)",
            "count": crit_wifi_count,
            "pct": round((crit_wifi_count / total_contracts_analyzed) * 100, 1) if total_contracts_analyzed > 0 else 0,
            "icon": "fa-wifi",
            "color": "#8b5cf6",
            "bg": "#f5f3ff",
        },
        "c2_suy_hao": {
            "label": "Suy hao không đạt chuẩn (< -23.5dBm)",
            "count": crit_suy_hao_count,
            "pct": round((crit_suy_hao_count / total_contracts_analyzed) * 100, 1) if total_contracts_analyzed > 0 else 0,
            "icon": "fa-signal",
            "color": "#ef4444",
            "bg": "#fef2f2",
        },
        "c3_rot": {
            "label": "Các lần kết nối không ổn định, Rớt kết nối",
            "count": crit_rot_count,
            "pct": round((crit_rot_count / total_contracts_analyzed) * 100, 1) if total_contracts_analyzed > 0 else 0,
            "icon": "fa-plug-circle-xmark",
            "color": "#f59e0b",
            "bg": "#fffbeb",
        },
        "c4_uptime": {
            "label": "Thiết bị chính lâu ngày chưa tắt mở",
            "count": crit_uptime_count,
            "pct": round((crit_uptime_count / total_contracts_analyzed) * 100, 1) if total_contracts_analyzed > 0 else 0,
            "icon": "fa-power-off",
            "color": "#0284c7",
            "bg": "#e0f2fe",
        },
    }

    # Backward compatibility error_groups
    error_groups = {
        "suy_hao": {"label": "Suy hao / Công suất thu kém (< -23.5dBm)", "count": crit_suy_hao_count, "icon": "fa-signal"},
        "rot_mang": {"label": "Rớt kết nối nhiều / Không ổn định", "count": crit_rot_count, "icon": "fa-plug-circle-xmark"},
        "wifi": {"label": "Sóng Wifi yếu (Thu phát kém)", "count": crit_wifi_count, "icon": "fa-wifi"},
        "uptime": {"label": "Thiết bị lâu ngày chưa tắt mở", "count": crit_uptime_count, "icon": "fa-power-off"},
        "need_action": {"label": "Tổng HĐ có cảnh báo vi phạm", "count": total_warn_contracts_all, "icon": "fa-wrench"},
        "warning": {"label": "Tổng số lượt cảnh báo 4 tiêu chí", "count": total_violations_all, "icon": "fa-triangle-exclamation"},
    }

    return jsonify({
        "success": True,
        "total_runs": total_runs,
        "total_contracts_analyzed": total_contracts_analyzed,
        "total_employees": len(assessment_list),
        "total_violations": total_violations_all,
        "total_warn_contracts": total_warn_contracts_all,
        "overall_pass_rate": overall_pass_rate,
        "criteria_summary": criteria_summary,
        "available_branches": available_branches,
        "current_branch": branch_filter,
        "employee_assessment": assessment_list,
        "error_groups": error_groups,
        "employee_leaderboard": assessment_list,
    })


@app.route("/api/progress")
def api_progress():
    """SSE endpoint - stream tiến trình và kết quả"""
    def generate():
        last_sent = 0
        while True:
            with _job_lock:
                progress         = _job_progress
                total            = _job_total
                percent          = _job_percent
                current_contract = _job_current_contract
                step_msg         = _job_step_msg
                step_num         = _job_step_num
                running          = _job_running
                status           = _job_status
                message          = _job_message
                new_results      = _job_results[last_sent:]

            payload = {
                "progress":         progress,
                "total":            total,
                "percent":          percent,
                "current_contract": current_contract,
                "step_msg":         step_msg,
                "step_num":         step_num,
                "running":          running,
                "status":           status,
                "message":          message,
                "new_results":      new_results,
            }
            last_sent += len(new_results)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            if not running and status in ("done", "error", "idle"):
                break
            time.sleep(1.0)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )





# ─────────────────────────────────────────────────────────────
#  PHÂN TÍCH HÀNH VI BẢO TRÌ
# ─────────────────────────────────────────────────────────────

def _parse_float(text):
    import re
    m = re.search(r'-?\d+(?:[.,]\d+)?', str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _extract_rx(result):
    """Lấy công suất thu (dBm) từ cong_suat_thu, fallback doi_chieu_tap_diem."""
    rx = _parse_float(result.get("cong_suat_thu"))
    if rx is None:
        import re
        m = re.search(r'Rx:\s*(-?\d+(?:[.,]\d+)?)', str(result.get("doi_chieu_tap_diem") or ""))
        if m:
            rx = float(m.group(1).replace(",", "."))
    return rx


def _count_bad_clients(result):
    import re
    client = str(result.get("phan_tich_client") or "").strip()
    if not client or client == "—":
        return 0
    nums = [int(x) for x in re.findall(r'(\d+)\s*thiết bị kém', client)]
    return max(nums) if nums else 0


def _snapshot(result):
    """Rút gọn các chỉ số thô của 1 lần phân tích."""
    return {
        "timestamp": result.get("_run_timestamp", ""),
        "rx": _extract_rx(result),
        "so_lan_rot": _parse_float(result.get("so_lan_rot")),
        "doi_chieu_rot_mang": str(result.get("doi_chieu_rot_mang") or "").strip(),
        "mo_hinh_mang": str(result.get("mo_hinh_mang") or "").strip(),
        "bad_clients": _count_bad_clients(result),
        "phan_tich_client": str(result.get("phan_tich_client") or "").strip(),
        "nhan_vien": result.get("nhan_vien", ""),
    }


RX_MIN, RX_MAX = -23.5, -10.0

_QUALITY_ACTIONS = {
    "suy_hao": "Yêu cầu xử lý lại suy hao",
    "ket_noi": "Kiểm tra lại FC / cáp lastmile / nguồn thiết bị / thay thiết bị kiểm tra",
    "mo_hinh": "Yêu cầu Swap Wifi 6",
    "wifi_kem": "Yêu cầu trang bị AP / Di dời modem",
}


def _grade_from_score(score):
    return {4: "rat_tot", 3: "dat", 2: "canh_bao", 1: "chua_dat", 0: "khan_cap"}.get(score, "khan_cap")


def _evaluate_360(before, after):
    """Chấm 4 tiêu chí 360 Quality dựa trên số liệu SAU bảo trì (so với TRƯỚC khi cần)."""
    criteria = {}

    # 1. Suy hao (Rx sau BT):
    #    - Rx > -10 (từ -10 đến 0.0 và số dương): MẤT TÍN HIỆU -> xử lý gấp (trước BT cũng vậy = tiếp tục mất tín hiệu)
    #    - -23.5 <= Rx <= -10: Đạt
    #    - Rx < -23.5: Suy hao -> xử lý lại
    a_rx, b_rx = after["rx"], before["rx"]
    if a_rx is None:
        criteria["suy_hao"] = {"pass": False, "label": "Không có dữ liệu", "detail": "Không đọc được công suất thu sau BT"}
    else:
        b_lost = b_rx is not None and b_rx > RX_MAX
        detail = f"Sau: {a_rx} dBm" + (f" | Trước: {b_rx} dBm" if b_rx is not None else "")
        if b_lost:
            detail += " (trước BT: Mất tín hiệu)"
        if a_rx > RX_MAX:
            label = "Tiếp tục mất tín hiệu" if b_lost else "Mất tín hiệu"
            criteria["suy_hao"] = {"pass": False, "label": label, "detail": detail,
                                   "action": "XỬ LÝ GẤP - " + label + ": kiểm tra FC / cáp lastmile / Modem"}
        elif RX_MIN <= a_rx <= RX_MAX:
            criteria["suy_hao"] = {"pass": True, "label": "Đạt", "detail": detail}
        else:
            detail += f" (ngoài ngưỡng {RX_MIN} → {RX_MAX} dBm)"
            criteria["suy_hao"] = {"pass": False, "label": "Suy hao", "detail": detail,
                                   "action": "Yêu cầu xử lý lại suy hao - Kiểm tra FC / cáp lastmile / Modem"}

    # 2. Kết nối: ổn định và số lần ra mạng < trước (hoặc sau = 0)
    rot = after["doi_chieu_rot_mang"]
    stable = ("Đảm bảo" in rot) and ("Chưa đảm bảo" not in rot) and ("Ra Mạng" not in rot)
    a_cnt = after["so_lan_rot"] or 0
    b_cnt = before["so_lan_rot"] or 0
    fewer = (a_cnt == 0) or (a_cnt < b_cnt)
    ok = stable and fewer
    detail = f"{rot or 'Không có dữ liệu'} | Ra mạng: sau {int(a_cnt)} / trước {int(b_cnt)}"
    criteria["ket_noi"] = {"pass": ok, "label": "Đạt" if ok else "Không đạt", "detail": detail}

    # 3. Mô hình mạng: phải bắt đầu bằng AX (Wifi 6)
    mh = after["mo_hinh_mang"]
    ok = mh.upper().startswith("AX")
    criteria["mo_hinh"] = {"pass": ok, "label": "Đạt" if ok else "Không đạt", "detail": mh or "Không có dữ liệu"}

    # 4. Thiết bị bắt sóng kém: không có thiết bị nào
    bad = after["bad_clients"]
    ok = bad == 0
    criteria["wifi_kem"] = {
        "pass": ok,
        "label": "Đạt" if ok else f"{bad} TB kém",
        "detail": after["phan_tich_client"] or "Không có TB kém",
    }

    for key, c in criteria.items():
        c["action"] = "" if c["pass"] else (c.get("action") or _QUALITY_ACTIONS[key])
    score = sum(1 for c in criteria.values() if c["pass"])
    return criteria, score


@app.route("/api/maintenance-behavior", methods=["POST"])
def api_maintenance_behavior():
    """
    360 Quality: chỉ đánh giá các HD có kết quả TRƯỚC bảo trì (ton_bao_tri),
    đối chiếu với kết quả SAU bảo trì (bao_tri) theo 4 tiêu chí.
    """
    try:
        data = request.get_json() or {}
        search_employee = str(data.get("employee", "")).strip().upper()
        search_hd = str(data.get("so_hd", "")).strip().upper()
        date_from = data.get("date_from", "")
        date_to = data.get("date_to", "")
        page = max(1, int(data.get("page", 1)))
        page_size = max(5, min(100, int(data.get("page_size", 20))))
        filter_result = data.get("filter_result", "")  # rat_tot|dat|canh_bao|chua_dat|khan_cap|chua_doi_chieu|""

        history = load_history()
        if not history:
            return jsonify({"success": True, "items": [], "total": 0, "page": 1, "pages": 0})

        # Gom TOÀN BỘ lịch sử theo HD (không lọc ngày/nhân viên ở mức run
        # để không làm mất một trong hai phía trước/sau)
        before_map, after_map = {}, {}
        for run in history:
            run_source = run.get("data_source", "")
            if run_source not in ("ton_bao_tri", "bao_tri"):
                continue
            run_ts = run.get("timestamp", "")
            for item in run.get("results", []):
                if item.get("status") == "error":
                    continue
                so_hd = (item.get("so_hd") or "").strip().upper()
                if not so_hd:
                    continue
                enriched = dict(item)
                enriched["_run_timestamp"] = run_ts
                enriched["_chi_nhanh"] = run.get("chi_nhanh", "")
                (before_map if run_source == "ton_bao_tri" else after_map).setdefault(so_hd, []).append(enriched)

        analyzed_items = []
        for so_hd, befores in before_map.items():  # chỉ HD có kết quả trước BT
            if search_hd and search_hd not in so_hd:
                continue

            latest_before = max(befores, key=lambda x: x.get("_run_timestamp", ""))
            before_ts = latest_before.get("_run_timestamp", "")
            # Lấy kết quả bảo trì mới nhất chạy SAU lần tồn bảo trì
            afters = [a for a in after_map.get(so_hd, []) if a.get("_run_timestamp", "") >= before_ts]
            latest_after = max(afters, key=lambda x: x.get("_run_timestamp", "")) if afters else None

            ref = latest_after or latest_before
            sides = [latest_before] + ([latest_after] if latest_after else [])

            def _date_ok(rec):
                d = rec.get("_run_timestamp", "")[:10]
                return not ((date_from and d < date_from) or (date_to and d > date_to))

            if (date_from or date_to) and not any(_date_ok(r) for r in sides):
                continue
            if search_employee and not any(
                search_employee in (r.get("nhan_vien") or "").strip().upper() for r in sides
            ):
                continue

            b_snap = _snapshot(latest_before)
            if latest_after:
                a_snap = _snapshot(latest_after)
                criteria, score = _evaluate_360(b_snap, a_snap)
                overall = _grade_from_score(score)
                actions = [c["action"] for c in criteria.values() if c["action"]]
            else:
                a_snap, criteria, score, overall, actions = None, {}, None, "chua_doi_chieu", []

            if filter_result and filter_result != overall:
                continue

            analyzed_items.append({
                "so_hd": so_hd,
                "nhan_vien": (latest_after or latest_before).get("nhan_vien", ""),
                "chi_nhanh": ref.get("_chi_nhanh", ""),
                "before": b_snap,
                "after": a_snap,
                "criteria": criteria,
                "score": score,
                "overall": overall,
                "actions": actions,
            })

        # Sắp xếp: khẩn cấp trước
        order = {"khan_cap": 0, "chua_dat": 1, "canh_bao": 2, "chua_doi_chieu": 3, "dat": 4, "rat_tot": 5}
        analyzed_items.sort(key=lambda x: order.get(x["overall"], 9))

        total = len(analyzed_items)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        start_idx = (page - 1) * page_size
        page_items = analyzed_items[start_idx:start_idx + page_size]

        stats = {"total": total}
        for k in ("rat_tot", "dat", "canh_bao", "chua_dat", "khan_cap", "chua_doi_chieu"):
            stats[k] = sum(1 for x in analyzed_items if x["overall"] == k)

        return jsonify({
            "success": True,
            "items": page_items,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "stats": stats,
        })

    except Exception as e:
        log.exception("Lỗi khi phân tích hành vi bảo trì")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/logout", methods=["POST"])
def api_logout():
    global _login_status
    close_driver()
    _login_status = "unknown"
    return jsonify({"success": True})


# ─────────────────────────────────────────────
#  LỊCH HẸN TOÀN TRÌNH: Import → Phân tích → Note
# ─────────────────────────────────────────────
from modules import scheduler


@app.route("/api/schedules", methods=["GET", "POST"])
def api_schedules():
    if request.method == "POST":
        data = request.get_json() or {}
        try:
            items = scheduler.save_schedules(data.get("schedules", []))
        except (ValueError, TypeError) as e:
            return jsonify({"success": False, "error": str(e)}), 400
        return jsonify({"success": True, "schedules": items})
    return jsonify({"success": True, "schedules": scheduler.load_schedules(),
                    "state": scheduler.get_state()})


@app.route("/api/schedules/status")
def api_schedules_status():
    return jsonify({"success": True, "state": scheduler.get_state()})


@app.route("/api/schedules/run_now", methods=["POST"])
def api_schedules_run_now():
    sid = (request.get_json() or {}).get("id")
    if not any(s["id"] == sid for s in scheduler.load_schedules()):
        return jsonify({"success": False, "error": "Không tìm thấy lịch"}), 404
    ok = scheduler.enqueue(sid)
    return jsonify({"success": ok, "message": "Đã xếp lịch vào hàng chạy" if ok else "Lịch đang chờ/chạy"})


def _sched_run_import(chi_nhanh):
    cfg = load_user_config()

    def on_file(path):
        return _import_ton_bao_tri_file(path, chi_nhanh)

    kwargs = {"totp_secret": cfg.get("totp_secret") or None}
    if cfg.get("mode") == "custom":
        kwargs.update(account=cfg.get("inside_account"), password=cfg.get("inside_password"),
                      use_otp=cfg.get("use_inside_otp", True))
    inside_import.run_auto_import(on_file, **kwargs)
    res = inside_import.get_state()
    if res["status"] == "done":
        return True, (res.get("result") or {}).get("message", "Import xong")
    return False, (res.get("result") or {}).get("error") or f"Import kết thúc: {res['status']}"


def _sched_post(path, payload):
    with app.test_client() as c:
        r = c.post(path, json=payload)
        return r.get_json() or {"success": False, "error": f"HTTP {r.status_code}"}


def _sched_job_state():
    with _job_lock:
        return {"running": _job_running, "status": _job_status, "progress": _job_progress,
                "total": _job_total, "results": list(_job_results)}


def _sched_ensure_login():
    """Đảm bảo đã đăng nhập hệ thống phân tích (tự đăng nhập nếu phiên hết hạn)."""
    global _login_status, _login_message
    if is_logged_in():
        _login_status = "ok"
        return True, "Đã đăng nhập sẵn"
    cfg = load_user_config()
    _login_status = "pending"
    try:
        if cfg.get("mode") == "custom":
            ok, msg = login(email_addr=cfg.get("email"), email_pass=cfg.get("email_password"),
                            step_callback=lambda m: log.info(f"Login Step: {m}"), otp_method="auto")
        else:
            ok, msg = login(step_callback=lambda m: log.info(f"Login Step: {m}"), otp_method="auto")
    except Exception as e:
        force_kill_driver()
        _login_status, _login_message = "error", str(e)
        return False, f"Lỗi đăng nhập: {e}"
    if ok and msg != "waiting_otp":
        _login_status, _login_message = "ok", msg
        return True, msg
    _login_status = "waiting_otp" if ok else "error"
    _login_message = msg
    return False, "Cần nhập OTP thủ công" if ok else msg


def start_scheduler():
    scheduler.start({
        "ensure_login": _sched_ensure_login,
        "busy": lambda: inside_import._running or inside_fpt._auto_note_running or _job_running,
        "run_import": _sched_run_import,
        "filter": lambda p: _sched_post("/api/filter", p),
        "job_state": _sched_job_state,
        "note": lambda p: _sched_post("/api/auto_note/start", p),
        "note_running": lambda: inside_fpt._auto_note_running,
    })


if __name__ == "__main__":
    start_scheduler()
    log.info(f"🚀 MYBAE AUTO Dashboard khởi động tại http://localhost:{FLASK_PORT}")
    def open_browser():
        time.sleep(1.2)
        import webbrowser
        webbrowser.open(f"http://localhost:{FLASK_PORT}")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, threaded=True)
