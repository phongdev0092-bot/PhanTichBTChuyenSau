"""
scheduler.py – Lịch hẹn chạy toàn trình tự động:
  Import Tồn Bảo Trì → Phân tích theo Đội Trưởng (>10 HĐ tự động chạy Rút gọn) → Auto Note.

Mỗi lịch: {id, time "HH:MM", days [0..6] (0=Thứ 2), chi_nhanh, captain (bắt buộc), enabled}.
Nhiều lịch có thể đến giờ cùng lúc → được xếp hàng và chạy lần lượt.
"""
import os
import json
import time
import queue
import logging
import threading
import uuid
from datetime import datetime

log = logging.getLogger("scheduler")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULE_FILE = os.path.join(BASE_DIR, "schedules.json")

COMPACT_THRESHOLD = 10  # > 10 HĐ → chạy rút gọn

_lock = threading.Lock()
_queue = queue.Queue()
_queued_ids = set()
_hooks = {}
_started = False
_state = {"running": False, "schedule_id": None, "step": "", "logs": []}


# ── Lưu trữ lịch ─────────────────────────────────────────────────────────────
def load_schedules() -> list:
    if not os.path.exists(SCHEDULE_FILE):
        return []
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        log.error(f"Lỗi đọc schedules.json: {e}")
        return []


def _save(items: list):
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _validate_time(t: str) -> str:
    try:
        return datetime.strptime(str(t).strip(), "%H:%M").strftime("%H:%M")
    except Exception:
        raise ValueError(f"Giờ không hợp lệ: '{t}' (định dạng HH:MM)")


def save_schedules(raw_items: list) -> list:
    """Thay toàn bộ danh sách lịch (hỗ trợ chọn nhiều lịch). Nếu có lịch → bắt buộc Đội Trưởng."""
    old = {s["id"]: s for s in load_schedules()}
    items = []
    for r in raw_items or []:
        captain = str(r.get("captain") or "").strip()
        chi_nhanh = str(r.get("chi_nhanh") or "").strip()
        if not captain:
            raise ValueError("Lịch bắt buộc phải chọn Đội Trưởng")
        if not chi_nhanh:
            raise ValueError("Lịch bắt buộc phải chọn Chi Nhánh")
        days = sorted({int(d) for d in (r.get("days") or range(7)) if 0 <= int(d) <= 6})
        sid = r.get("id") or uuid.uuid4().hex[:8]
        prev = old.get(sid, {})
        items.append({
            "id": sid,
            "time": _validate_time(r.get("time")),
            "days": days or list(range(7)),
            "chi_nhanh": chi_nhanh,
            "captain": captain,
            "enabled": bool(r.get("enabled", True)),
            "last_run": prev.get("last_run", ""),
            "last_status": prev.get("last_status", ""),
        })
    with _lock:
        _save(items)
    return items


def _update_schedule(sid: str, **fields):
    with _lock:
        items = load_schedules()
        for s in items:
            if s["id"] == sid:
                s.update(fields)
        _save(items)


# ── Log / trạng thái ─────────────────────────────────────────────────────────
def _log(msg: str, level: str = "info"):
    _state["logs"].append({"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
    del _state["logs"][:-300]
    getattr(log, level if level in ("info", "warning", "error") else "info")(msg)


STEP_KEYS = ("import", "login", "analyze", "note")


def _reset_steps():
    _state["steps"] = {k: {"status": "pending", "detail": ""} for k in STEP_KEYS}


def _step(key: str, status: str, detail: str = ""):
    _state["steps"][key] = {"status": status, "detail": detail}
    if status == "running":
        _state["step"] = key


_reset_steps()
_state.update(schedule=None, last=None)


def get_state() -> dict:
    return {**_state, "logs": list(_state["logs"]), "steps": dict(_state["steps"]),
            "queued": len(_queued_ids)}


# ── Pipeline ─────────────────────────────────────────────────────────────────
def run_pipeline(sched: dict) -> tuple[bool, str]:
    """Chạy tuần tự: import → đăng nhập → phân tích → note. Trả về (ok, message)."""
    h = _hooks
    chi_nhanh, captain = sched["chi_nhanh"], sched["captain"]
    _log(f"▶ Bắt đầu lịch {sched['time']} – CN '{chi_nhanh}' – Đội trưởng '{captain}'")

    # 1. Import tự động
    _step("import", "running", "Đang chờ tiến trình khác..." if h["busy"]() else "Đang import từ Inside")
    if h["busy"]():
        _log("Có tiến trình khác đang chạy, đợi...", "warning")
        while h["busy"]():
            time.sleep(5)
        _step("import", "running", "Đang import từ Inside")
    ok, msg = h["run_import"](chi_nhanh)
    _log(msg, "info" if ok else "error")
    if not ok:
        _step("import", "error", msg)
        return False, f"Import thất bại: {msg}"
    _step("import", "done", msg)

    # 2. Đăng nhập hệ thống phân tích (nếu chưa/đã hết phiên)
    _step("login", "running", "Kiểm tra phiên đăng nhập")
    ok, msg = h["ensure_login"]()
    _log(("Đăng nhập: " if ok else "Đăng nhập thất bại: ") + str(msg), "info" if ok else "error")
    if not ok:
        _step("login", "error", str(msg))
        return False, f"Không đăng nhập được để phân tích: {msg}"
    _step("login", "done", str(msg))

    # 3. Phân tích theo đội trưởng
    _step("analyze", "running", "Đang lấy danh sách HĐ")
    payload = {"employees": [], "team_captain": captain, "chi_nhanh": chi_nhanh,
               "data_source": "ton_bao_tri"}
    count = h["filter"]({**payload, "check_count_only": True})
    if not count.get("success"):
        _step("analyze", "error", str(count.get("error")))
        return False, f"Lỗi kiểm tra HĐ: {count.get('error')}"
    total = count.get("total", 0)
    if total == 0:
        m = count.get("message") or "Không có HĐ cần phân tích"
        _step("analyze", "skipped", m)
        _step("note", "skipped", "Không có HĐ")
        return True, m
    mode = "compact" if total > COMPACT_THRESHOLD else "full"
    mode_txt = "RÚT GỌN" if mode == "compact" else "ĐẦY ĐỦ"
    _log(f"Có {total} HĐ → chạy phân tích chế độ {mode_txt}")
    res = h["filter"]({**payload, "analysis_mode": mode})
    if not res.get("success"):
        _step("analyze", "error", str(res.get("error")))
        return False, f"Không chạy được phân tích: {res.get('error')}"
    while True:
        job = h["job_state"]()
        _step("analyze", "running", f"{job['progress']}/{job['total']} HĐ – chế độ {mode_txt}")
        if not job["running"]:
            break
        time.sleep(3)
    if job["status"] != "done":
        _step("analyze", "error", f"Trạng thái: {job['status']}")
        return False, f"Phân tích kết thúc với trạng thái: {job['status']}"
    _step("analyze", "done", f"{len(job['results'])} HĐ – chế độ {mode_txt}")
    _log(f"✔ Phân tích xong {len(job['results'])} HĐ")

    # 4. Auto Note
    _step("note", "running", "Đang khởi động Auto Note")
    note = h["note"]({"results": job["results"]})
    if not note.get("success"):
        # Không có HĐ đủ điều kiện note không phải là lỗi của flow
        _log(f"Bỏ qua note: {note.get('error')}", "warning")
        _step("note", "skipped", str(note.get("error")))
        return True, f"Phân tích {len(job['results'])} HĐ, không có HĐ cần note"
    _log(note.get("message", "Đang note..."))
    _step("note", "running", f"Đang note {note.get('total', 0)} HĐ")
    time.sleep(5)
    while h["note_running"]():
        time.sleep(5)
    _step("note", "done", f"{note.get('total', 0)} HĐ")
    return True, f"Hoàn tất: import → phân tích {len(job['results'])} HĐ → note {note.get('total', 0)} HĐ"


def _worker():
    while True:
        sid = _queue.get()
        sched = next((s for s in load_schedules() if s["id"] == sid), None)
        if not sched:
            _queued_ids.discard(sid)
            continue
        _reset_steps()
        _state.update(running=True, schedule_id=sid, step="", logs=[], last=None,
                      schedule={k: sched[k] for k in ("time", "chi_nhanh", "captain")},
                      started_at=datetime.now().strftime("%H:%M:%S"))
        try:
            ok, msg = run_pipeline(sched)
        except Exception as e:
            log.exception("Lỗi pipeline lịch")
            ok, msg = False, f"Lỗi: {e}"
            cur = _state.get("step")
            if cur in _state["steps"] and _state["steps"][cur]["status"] == "running":
                _step(cur, "error", str(e))
        _log(("✅ " if ok else "❌ ") + msg, "info" if ok else "error")
        _update_schedule(sid, last_run=datetime.now().strftime("%Y-%m-%d %H:%M"),
                         last_status=("OK: " if ok else "LỖI: ") + msg)
        _state.update(running=False, schedule_id=None, step="",
                      last={"ok": ok, "message": msg, "at": datetime.now().strftime("%H:%M:%S")})
        _queued_ids.discard(sid)


def enqueue(sid: str) -> bool:
    if sid in _queued_ids:
        return False
    _queued_ids.add(sid)
    _queue.put(sid)
    return True


def _ticker():
    while True:
        now = datetime.now()
        hhmm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
        for s in load_schedules():
            if (s.get("enabled") and s["time"] == hhmm and now.weekday() in s["days"]
                    and not str(s.get("last_run", "")).startswith(f"{today} {hhmm}")):
                _update_schedule(s["id"], last_run=f"{today} {hhmm}", last_status="Đang chờ chạy...")
                enqueue(s["id"])
        time.sleep(20)


def start(hooks: dict):
    """hooks: busy(), run_import(chi_nhanh)->(ok,msg), filter(payload)->dict,
    job_state()->dict, note(payload)->dict, note_running()->bool"""
    global _started
    if _started:
        return
    _started = True
    _hooks.update(hooks)
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_ticker, daemon=True).start()
    log.info("Scheduler đã khởi động")
