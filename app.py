"""
app.py - MYBAE AUTO Dashboard - Flask Server
"""
import json
import threading
import time
import logging
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

from config import FLASK_PORT, SECRET_KEY
from modules.auth import login, is_logged_in, close_driver
from modules.sheet_reader import get_employees, get_contracts, get_team_captains, get_cll30_analytics
from modules.analyzer import analyze_contract

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

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


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    global _login_status
    if _login_status == "ok":
        return jsonify({"success": True, "message": "✅ Đã đăng nhập"})

    _login_status = "pending"
    def do_login():
        global _login_status
        ok, msg = login()
        _login_status = "ok" if ok else "error"
        log.info(f"Login: {msg}")

    t = threading.Thread(target=do_login, daemon=True)
    t.start()
    return jsonify({"success": True, "message": "⏳ Đang đăng nhập..."})


@app.route("/api/login_status")
def api_login_status():
    return jsonify({"status": _login_status})


@app.route("/api/employees")
def api_employees():
    try:
        employees = get_employees()
        return jsonify({"success": True, "employees": employees})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/team_captains")
def api_team_captains():
    try:
        data = get_team_captains()
        return jsonify({"success": True, "captains": data.get("sorted_captains", []), "captains_map": data.get("captains_map", {})})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/cll30_top10")
def api_cll30_top10():
    try:
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        captain = request.args.get("captain")
        data = get_cll30_analytics(start_date=start_date, end_date=end_date, top_n=10, selected_captain=captain)
        return jsonify({"success": True, "data": data})
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

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Error reading history: {e}")
        return []

def save_history_run(run_data):
    history = load_history()
    history.insert(0, run_data)
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
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
    if not team_captain:
        team_captain = None

    # Xử lý ghép danh sách nhân viên:
    final_selected = set(selected)
    
    # 1. Nếu chọn Đội Trưởng, gom nhân viên thuộc Đội Trưởng
    if team_captain:
        captains_data = get_team_captains()
        captain_members = captains_data.get("captains_map", {}).get(team_captain, [])
        if captain_members:
            final_selected.update(captain_members)

    # 2. Phân tích TOP 10 CLL30N trong tháng đang xét
    cll30_analytics = get_cll30_analytics(start_date=start_date, end_date=end_date, top_n=10, selected_captain=team_captain)
    top10_list = cll30_analytics.get("top_n", [])

    # Khi CHỌN ĐỘI TRƯỞNG: Nếu nhân sự thuộc TOP 10 thuộc quyền quản lý của đội trưởng đó thì mặc định chạy kèm.
    # Khi CHỈ CHỌN NHÂN VIÊN (team_captain is None): KHÔNG chạy kèm bất kỳ TOP 10 nào.
    auto_added_top10 = []
    if team_captain:
        for item in top10_list:
            emp = item["nhan_vien"]
            if item.get("is_captain_member"):
                final_selected.add(emp)
                auto_added_top10.append(emp)

    # Lấy danh sách hợp đồng
    try:
        contracts = get_contracts(list(final_selected), start_date=start_date, end_date=end_date)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    if not contracts:
        return jsonify({
            "success": True,
            "total": 0,
            "contracts": [],
            "cll30_analytics": cll30_analytics,
            "auto_added_top10": auto_added_top10,
            "message": "Không tìm thấy hợp đồng nào phù hợp trong khoảng thời gian đã chọn."
        })

    # Reset cờ ngắt tiến trình
    _job_cancel_requested = False

    # Bắt đầu job chạy nền
    with _job_lock:
        _job_results          = []
        _job_progress         = 0
        _job_total            = len(contracts)
        _job_running          = True
        _job_status           = "running"
        _job_message          = f"Đang phân tích {len(contracts)} hợp đồng..."
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
                log.info(f"[{i+1}/{total_cnt}] Phân tích HĐ: {contract_code}")

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
                    res = analyze_contract(contract_code, c.get("tg_hoan_tat", ""), step_callback=step_cb)
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
                        "phien_ban_pm": "",
                        "dns_wan": "",
                        "doi_chieu_rot_mang": "",
                        "doi_chieu_tap_diem": "",
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

    return jsonify({
        "success":  True,
        "total":    len(contracts),
        "cll30_analytics": cll30_analytics,
        "auto_added_top10": auto_added_top10,
        "message":  f"Bắt đầu phân tích {len(contracts)} hợp đồng",
    })



@app.route("/api/history", methods=["GET"])
def api_get_history():
    return jsonify({"success": True, "history": load_history()})


@app.route("/api/history", methods=["DELETE"])
def api_clear_history():
    try:
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
        return jsonify({"success": True, "message": "Đã xóa lịch sử"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/analytics")
def api_analytics():
    history = load_history()

    all_runs_results = []
    for run in history:
        all_runs_results.extend(run.get("results", []))

    total_contracts_analyzed = len(all_runs_results)

    error_groups = {
        "suy_hao": {"label": "Suy hao / Công suất thu kém", "count": 0, "icon": "fa-signal"},
        "rot_mang": {"label": "Rớt kết nối nhiều", "count": 0, "icon": "fa-plug-circle-xmark"},
        "dns_wan": {"label": "Lỗi DNS WAN / Mạng", "count": 0, "icon": "fa-network-wired"},
        "auto_resolved": {"label": "Tự động xử lý lỗi", "count": 0, "icon": "fa-robot"},
        "need_action": {"label": "Cần kỹ thuật xử lý", "count": 0, "icon": "fa-wrench"},
        "warning": {"label": "Cảnh báo hệ thống", "count": 0, "icon": "fa-triangle-exclamation"},
    }

    emp_map = {}

    for r in all_runs_results:
        nv = (r.get("nhan_vien") or "KXD").strip()
        if nv not in emp_map:
            emp_map[nv] = {
                "nhan_vien": nv,
                "total_contracts": 0,
                "need_count": 0,
                "warn_count": 0,
                "auto_count": 0,
                "suy_hao_count": 0,
                "rot_count": 0,
            }

        emp_map[nv]["total_contracts"] += 1

        need = r.get("can_xu_ly")
        warn = r.get("canh_bao")
        auto = r.get("xu_ly_loi_tu_dong")
        pwr  = r.get("cong_suat_thu")
        drop = r.get("so_lan_rot")

        if need and need != "—":
            error_groups["need_action"]["count"] += 1
            emp_map[nv]["need_count"] += 1

        if warn and warn != "—":
            error_groups["warning"]["count"] += 1
            emp_map[nv]["warn_count"] += 1

        if auto and auto != "—":
            error_groups["auto_resolved"]["count"] += 1
            emp_map[nv]["auto_count"] += 1

        try:
            val = float(pwr)
            if val < -25:
                error_groups["suy_hao"]["count"] += 1
                emp_map[nv]["suy_hao_count"] += 1
        except (ValueError, TypeError):
            pass

        try:
            val = int(drop)
            if val > 0:
                error_groups["rot_mang"]["count"] += 1
                emp_map[nv]["rot_count"] += 1
        except (ValueError, TypeError):
            pass

        dns = r.get("dns_wan") or ""
        if "loi" in dns.lower() or "error" in dns.lower() or dns in ("0.0.0.0", "None"):
            error_groups["dns_wan"]["count"] += 1

    employees_leaderboard = sorted(
        emp_map.values(),
        key=lambda x: (x["need_count"] + x["warn_count"], x["suy_hao_count"] + x["rot_count"]),
        reverse=True
    )

    return jsonify({
        "success": True,
        "total_runs": len(history),
        "total_contracts_analyzed": total_contracts_analyzed,
        "error_groups": error_groups,
        "employee_leaderboard": employees_leaderboard,
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


@app.route("/api/logout", methods=["POST"])
def api_logout():
    global _login_status
    close_driver()
    _login_status = "unknown"
    return jsonify({"success": True})


if __name__ == "__main__":
    log.info(f"🚀 MYBAE AUTO Dashboard khởi động tại http://localhost:{FLASK_PORT}")
    def open_browser():
        time.sleep(1.2)
        import webbrowser
        webbrowser.open(f"http://localhost:{FLASK_PORT}")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, threaded=True)
