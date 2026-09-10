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
from modules.sheet_reader import get_employees, get_contracts
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
_job_status    = "idle"   # idle | logging_in | running | done | error
_job_message   = ""
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


@app.route("/api/filter", methods=["POST"])
def api_filter():
    global _job_results, _job_progress, _job_total, _job_running, _job_status, _job_message

    data = request.get_json()
    selected = data.get("employees", [])

    if not selected:
        return jsonify({"success": False, "error": "Chưa chọn nhân viên"}), 400

    if _job_running:
        return jsonify({"success": False, "error": "Đang xử lý, vui lòng chờ"}), 429

    # Lấy danh sách hợp đồng
    try:
        contracts = get_contracts(selected)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    if not contracts:
        return jsonify({
            "success": True,
            "total": 0,
            "contracts": [],
            "message": "Không có hợp đồng nào trong khoảng Today-1 / Today-2. Hệ thống đã kiểm tra thêm 7 ngày gần nhất."
        })

    # Bắt đầu job chạy nền
    with _job_lock:
        _job_results  = []
        _job_progress = 0
        _job_total    = len(contracts)
        _job_running  = True
        _job_status   = "running"
        _job_message  = f"Đang phân tích {len(contracts)} hợp đồng..."

    def run_job():
        global _job_results, _job_progress, _job_running, _job_status, _job_message
        try:
            for i, c in enumerate(contracts):
                log.info(f"[{i+1}/{len(contracts)}] Phân tích HĐ: {c['so_hd']}")
                try:
                    res = analyze_contract(c["so_hd"])
                except Exception as exc:
                    log.exception(f"Lỗi khi phân tích HĐ {c['so_hd']}")
                    res = {
                        "so_hd": c["so_hd"],
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

            with _job_lock:
                _job_running = False
                _job_status  = "done"
                _job_message = f"Hoàn tất {len(contracts)} hợp đồng"

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
        "message":  f"Bắt đầu phân tích {len(contracts)} hợp đồng",
    })


@app.route("/api/progress")
def api_progress():
    """SSE endpoint - stream tiến trình và kết quả"""
    def generate():
        last_sent = 0
        while True:
            with _job_lock:
                progress  = _job_progress
                total     = _job_total
                running   = _job_running
                status    = _job_status
                message   = _job_message
                new_results = _job_results[last_sent:]

            payload = {
                "progress": progress,
                "total":    total,
                "running":  running,
                "status":   status,
                "message":  message,
                "new_results": new_results,
            }
            last_sent += len(new_results)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            if not running and status in ("done", "error", "idle"):
                break
            time.sleep(1.5)

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
