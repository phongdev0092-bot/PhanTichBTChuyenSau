"""
inside_import.py – Import tự động Tồn Bảo Trì từ FPT Inside

Luồng:
  1. Đăng nhập inside.fpt.net (dùng lại login_inside_fpt của Auto Note)
  2. Inside New V5 → Hỗ trợ kỹ thuật → Thông tin chung → tab BẢO TRÌ (navigate_to_bao_tri)
  3. Từ ngày = ngày 1 tháng trước ... Đến ngày = ngày cuối tháng hiện tại
  4. Tab TỒN POP → Xem báo cáo
  5. Bấm vào số lượng → Xuất Excel → chờ tải xong
  6. Gọi callback on_file(path) để import file lên Supabase (bảng ton_bao_tri)
"""
import os
import time
import logging
import calendar
from datetime import date

from selenium.webdriver.common.by import By

from modules import inside_fpt

log = logging.getLogger("inside_import")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads", "inside_import")
DEBUG_DIR = os.path.join(BASE_DIR, "debug_shots")

# ── Trạng thái job ───────────────────────────────────────────────────────────
_running = False
_status = "idle"      # idle | logging_in | navigating | filtering | exporting | importing | done | error | cancelled
_cancel = False
_percent = 0
_logs = []
_result = {}


class _Cancelled(Exception):
    pass


def _log(msg: str, level: str = "info"):
    _logs.append({"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
    getattr(log, "error" if level == "error" else "warning" if level == "warning" else "info")(msg)


def _check_cancel():
    if _cancel:
        raise _Cancelled()


def _set(status: str = None, percent: int = None):
    global _status, _percent
    if status is not None:
        _status = status
    if percent is not None:
        _percent = percent


def get_state() -> dict:
    return {
        "running": _running,
        "status": _status,
        "percent": _percent,
        "logs": list(_logs),
        "result": dict(_result),
    }


def cancel():
    global _cancel
    _cancel = True
    _log("⛔ Nhận lệnh dừng từ người dùng...", "warning")


# ── Khoảng ngày ──────────────────────────────────────────────────────────────
def get_date_range(today: date = None) -> tuple[date, date]:
    """Ngày đầu tiên của tháng trước → ngày cuối cùng của tháng hiện tại."""
    today = today or date.today()
    if today.month == 1:
        first = date(today.year - 1, 12, 1)
    else:
        first = date(today.year, today.month - 1, 1)
    last = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    return first, last


# ── Tiện ích Selenium (tìm phần tử xuyên frame) ──────────────────────────────
def _search_frames(d, xpath, visible=True, depth=0, max_depth=3):
    """Tìm phần tử trong frame hiện tại rồi đệ quy vào iframe con. Giữ nguyên frame khi tìm thấy."""
    try:
        for el in d.find_elements(By.XPATH, xpath):
            try:
                if not visible or el.is_displayed():
                    return el
            except Exception:
                continue
    except Exception:
        pass
    if depth >= max_depth:
        return None
    try:
        n = len(d.find_elements(By.CSS_SELECTOR, "iframe,frame"))
    except Exception:
        return None
    for i in range(n):
        try:
            frames = d.find_elements(By.CSS_SELECTOR, "iframe,frame")
            d.switch_to.frame(frames[i])
        except Exception:
            continue
        found = _search_frames(d, xpath, visible, depth + 1, max_depth)
        if found is not None:
            return found
        try:
            d.switch_to.parent_frame()
        except Exception:
            d.switch_to.default_content()
            return None
    return None


def _locate(d, xpath, timeout=20, visible=True):
    end = time.time() + timeout
    while time.time() < end:
        _check_cancel()
        try:
            d.switch_to.default_content()
        except Exception:
            pass
        el = _search_frames(d, xpath, visible)
        if el is not None:
            return el
        time.sleep(1)
    return None


_FIND_TEXT_JS = """
var targets = arguments[0], exact = arguments[1];
var best = null, bestLen = 1e9;
document.querySelectorAll('body *').forEach(function(el){
  if (el.offsetWidth === 0 || el.offsetHeight === 0) return;
  var tag = el.tagName;
  if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'OPTION') return;
  var t = (el.textContent || '').replace(/\\s+/g, ' ').trim().toUpperCase();
  if (!t || t.length > 60) return;
  for (var i = 0; i < targets.length; i++) {
    var x = targets[i];
    var ok = exact ? (t === x) : (t.indexOf(x) >= 0);
    if (ok && t.length <= bestLen) { best = el; bestLen = t.length; }
  }
});
return best;
"""

_FIND_NUMBER_JS = """
var best = null, bestVal = 0;
document.querySelectorAll('body *').forEach(function(el){
  if (el.children.length > 0 || el.offsetWidth === 0 || el.offsetHeight === 0) return;
  var t = (el.textContent || '').trim();
  if (!/^[0-9][0-9.,]*$/.test(t)) return;
  var v = parseInt(t.replace(/[.,]/g, ''), 10);
  if (!(v > 0)) return;
  var clickable = el.closest('a') || window.getComputedStyle(el).cursor === 'pointer' ||
                  el.onclick || el.closest('[onclick]');
  if (!clickable) return;
  if (v > bestVal) { best = el; bestVal = v; }
});
return best;
"""


def _find_text(d, texts, exact=True, timeout=20):
    """Tìm phần tử hiển thị theo chữ (không phân biệt hoa thường), duyệt mọi frame."""
    targets = [t.upper() for t in texts]
    end = time.time() + timeout
    while time.time() < end:
        _check_cancel()
        d.switch_to.default_content()
        el = _walk_frames(d, lambda drv: drv.execute_script(_FIND_TEXT_JS, targets, exact))
        if el is not None:
            return el
        time.sleep(1)
    return None


def _find_number(d):
    d.switch_to.default_content()
    return _walk_frames(d, lambda drv: drv.execute_script(_FIND_NUMBER_JS))


_SAVE_PICKER_STUB_JS = """
if (!window.__mybaePickerStubbed) {
  window.__mybaePickerStubbed = true;
  window.showSaveFilePicker = async function(opts) {
    var name = (opts && opts.suggestedName) || 'export.xlsx';
    var chunks = [];
    return {
      kind: 'file',
      name: name,
      createWritable: async function() {
        return {
          write: async function(data) {
            if (data && data.type === 'write' && data.data !== undefined) { data = data.data; }
            chunks.push(data);
          },
          seek: async function() {},
          truncate: async function() {},
          abort: async function() {},
          close: async function() {
            var blob = new Blob(chunks, {type: 'application/octet-stream'});
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = name;
            document.body.appendChild(a);
            a.click();
            setTimeout(function(){ a.remove(); }, 2000);
          }
        };
      },
      getFile: async function() { return new File(chunks, name); }
    };
  };
}
return true;
"""


def _accept_alerts(d, timeout=10) -> bool:
    """Bấm OK popup JS (alert/confirm) nếu xuất hiện trong khoảng timeout. Trả về True nếu đã bấm."""
    end = time.time() + timeout
    accepted = False
    while time.time() < end:
        try:
            alert = d.switch_to.alert
            _log(f"Popup: {alert.text}")
            alert.accept()
            accepted = True
            time.sleep(1)  # có thể còn popup kế tiếp
            continue
        except Exception:
            if accepted:
                return True
        time.sleep(0.5)
    return accepted


def _click(d, el):
    from selenium.common.exceptions import UnexpectedAlertPresentException
    try:
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except UnexpectedAlertPresentException:
        return
    except Exception:
        pass
    try:
        el.click()
    except UnexpectedAlertPresentException:
        return
    except Exception:
        try:
            d.execute_script("arguments[0].click();", el)
        except UnexpectedAlertPresentException:
            return


def _shot(d, name):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        d.save_screenshot(os.path.join(DEBUG_DIR, name))
    except Exception:
        pass


_FIND_DATE_JS = """
var out = [];
var re = /^\\d{2}\\/\\d{2}\\/\\d{4}$/;
document.querySelectorAll('input').forEach(function(el){
  var v = (el.value || '').trim();
  var vis = el.offsetWidth > 0 && el.offsetHeight > 0;
  if (vis && (el.type === 'date' || re.test(v))) { out.push(el); }
});
return out;
"""

_DUMP_INPUTS_JS = """
return Array.prototype.map.call(document.querySelectorAll('input'), function(e){
  return [e.id, e.name, e.type, e.className, e.value, e.readOnly, e.offsetWidth>0].join('|');
}).slice(0, 40);
"""

_SET_DATE_JS = """
var el = arguments[0], dmy = arguments[1], iso = arguments[2];
var isDateType = (el.type === 'date');
var val = isDateType ? iso : dmy;
var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
if (setter && setter.set) { setter.set.call(el, val); } else { el.value = val; }
try {
  var jq = window.jQuery || window.$;
  if (jq) {
    var $el = jq(el);
    var parts = dmy.split('/');
    var dt = new Date(parseInt(parts[2]), parseInt(parts[1]) - 1, parseInt(parts[0]));
    var kendo = $el.data && $el.data('kendoDatePicker');
    if (kendo) { kendo.value(dt); kendo.trigger('change'); }
    else if ($el.datepicker) { try { $el.datepicker('setDate', dt); } catch (e) {} }
    $el.trigger('change');
  }
} catch (e) {}
['input','change','blur'].forEach(function(t){ el.dispatchEvent(new Event(t, {bubbles:true})); });
return el.value;
"""


def _walk_frames(d, fn, depth=0, max_depth=3):
    """Chạy fn(d) ở frame hiện tại rồi đệ quy vào iframe con; dừng và giữ frame khi fn trả về truthy."""
    try:
        r = fn(d)
        if r:
            return r
    except Exception:
        pass
    if depth >= max_depth:
        return None
    try:
        n = len(d.find_elements(By.CSS_SELECTOR, "iframe,frame"))
    except Exception:
        return None
    for i in range(n):
        try:
            d.switch_to.frame(d.find_elements(By.CSS_SELECTOR, "iframe,frame")[i])
        except Exception:
            continue
        r = _walk_frames(d, fn, depth + 1, max_depth)
        if r:
            return r
        try:
            d.switch_to.parent_frame()
        except Exception:
            d.switch_to.default_content()
            return None
    return None


def _set_date_range(d, d_from: date, d_to: date) -> bool:
    def finder(drv):
        els = drv.execute_script(_FIND_DATE_JS)
        return els if els and len(els) >= 2 else None

    inputs = None
    end = time.time() + 25
    while time.time() < end and not inputs:
        _check_cancel()
        d.switch_to.default_content()
        inputs = _walk_frames(d, finder)
        if not inputs:
            time.sleep(1)
    if not inputs:
        try:
            d.switch_to.default_content()
            dump = _walk_frames(d, lambda drv: drv.execute_script(_DUMP_INPUTS_JS) or None)
            _log("Danh sách input tìm thấy: " + " ; ".join(dump or []), "warning")
        except Exception:
            pass
        return False

    f_dmy, t_dmy = d_from.strftime("%d/%m/%Y"), d_to.strftime("%d/%m/%Y")

    # Cách 1 (giống người dùng): bấm icon lịch → chọn tháng → bấm ngày
    for inp, target, dmy in ((inputs[0], d_from, f_dmy), (inputs[1], d_to, t_dmy)):
        try:
            if not _pick_date(d, inp, target):
                _log(f"Chọn lịch cho {dmy} chưa thành công, thử ghi trực tiếp giá trị...", "warning")
                d.execute_script(_SET_DATE_JS, inp, dmy, target.isoformat())
        except _Cancelled:
            raise
        except Exception as e:
            _log(f"Lỗi khi chọn ngày {dmy}: {e}", "warning")
            d.execute_script(_SET_DATE_JS, inp, dmy, target.isoformat())

    v_from = d.execute_script("return arguments[0].value;", inputs[0])
    v_to = d.execute_script("return arguments[0].value;", inputs[1])
    _log(f"Đã đặt Từ ngày = {v_from} | Đến ngày = {v_to}")
    return v_from in (f_dmy, d_from.isoformat()) and v_to in (t_dmy, d_to.isoformat())


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}

# Tìm tiêu đề lịch dạng "2026 October" đang hiển thị
_CAL_HEADER_JS = """
var re = /^\\d{4}\\s+[A-Za-z]+$/;
var best = null;
document.querySelectorAll('*').forEach(function(el){
  if (el.children.length > 0) return;
  var t = (el.textContent || '').trim();
  if (re.test(t) && el.offsetWidth > 0 && el.offsetHeight > 0) { best = el; }
});
return best;
"""

# Click phần tử nhỏ nằm bên trái (dir=-1) / bên phải (dir=1) tiêu đề lịch để đổi tháng
_CAL_NAV_JS = """
var h = arguments[0], dir = arguments[1];
var hr = h.getBoundingClientRect();
var root = h;
for (var i = 0; i < 6 && root.parentElement; i++) {
  root = root.parentElement;
  if ((root.textContent || '').indexOf('Sun') >= 0) break;
}
var cands = [];
root.querySelectorAll('*').forEach(function(el){
  var r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0 || r.width > 60 || r.height > 60) return;
  var cy = r.top + r.height / 2;
  if (Math.abs(cy - (hr.top + hr.height / 2)) > 20) return;
  if (dir < 0 && r.right <= hr.left) cands.push([r, el]);
  if (dir > 0 && r.left >= hr.right) cands.push([r, el]);
});
if (!cands.length) return false;
cands.sort(function(a, b){ return dir < 0 ? b[0].right - a[0].right : a[0].left - b[0].left; });
var r = cands[0][0];
var target = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) || cands[0][1];
['mousedown','mouseup','click'].forEach(function(t){
  target.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window}));
});
return true;
"""

# Bấm vào ô ngày (số) trong bảng lịch
_CAL_DAY_JS = """
var h = arguments[0], day = String(arguments[1]);
var root = h;
for (var i = 0; i < 6 && root.parentElement; i++) {
  root = root.parentElement;
  if ((root.textContent || '').indexOf('Sun') >= 0) break;
}
var hr = h.getBoundingClientRect();
var found = null;
root.querySelectorAll('*').forEach(function(el){
  if (found || el.children.length > 0) return;
  if ((el.textContent || '').trim() !== day) return;
  var r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0 || r.top <= hr.bottom) return;
  var cs = window.getComputedStyle(el);
  var cls = (el.className && el.className.toString ? el.className.toString() : '') + ' ' +
            ((el.parentElement && el.parentElement.className) ? el.parentElement.className.toString() : '');
  if (/other|outside|disabled|off|prev|next/i.test(cls)) return;
  found = el;
});
if (!found) return false;
['mousedown','mouseup','click'].forEach(function(t){
  found.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window}));
});
return true;
"""


def _pick_date(d, inp, target: date) -> bool:
    """Mở lịch của ô `inp` bằng icon, chuyển đúng tháng/năm rồi bấm ngày `target`."""
    from selenium.webdriver.common.action_chains import ActionChains

    def header():
        return d.execute_script(_CAL_HEADER_JS)

    def open_calendar():
        d.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
        w = inp.size.get("width", 100)
        # Lần 1: bấm vào icon lịch (sát mép phải ô nhập); lần 2: bấm vào chính ô nhập
        for off in (int(w / 2) - 14, 0):
            try:
                ActionChains(d).move_to_element_with_offset(inp, off, 0).click().perform()
            except Exception:
                d.execute_script("arguments[0].click();", inp)
            time.sleep(0.8)
            h = header()
            if h is not None:
                return h
        return None

    h = open_calendar()
    if h is None:
        return False

    for _ in range(36):  # tối đa 3 năm
        _check_cancel()
        h = header()
        if h is None:
            return False
        parts = h.text.strip().split()
        try:
            cur_year, cur_month = int(parts[0]), _MONTHS[parts[1].lower()]
        except Exception:
            return False
        diff = (target.year - cur_year) * 12 + (target.month - cur_month)
        if diff == 0:
            break
        if not d.execute_script(_CAL_NAV_JS, h, -1 if diff < 0 else 1):
            return False
        time.sleep(0.4)
    else:
        return False

    h = header()
    if h is None or not d.execute_script(_CAL_DAY_JS, h, target.day):
        return False
    time.sleep(0.8)
    val = d.execute_script("return arguments[0].value;", inp)
    return val in (target.strftime("%d/%m/%Y"), target.isoformat())


def _wait_download(before: set, timeout=300) -> str | None:
    """Chờ file Excel mới xuất hiện trong thư mục tải xuống và tải xong."""
    end = time.time() + timeout
    last_size, stable = {}, 0
    while time.time() < end:
        _check_cancel()
        files = set(os.listdir(DOWNLOAD_DIR)) - before
        done = [f for f in files if not f.lower().endswith((".crdownload", ".tmp"))]
        partial = [f for f in files if f.lower().endswith((".crdownload", ".tmp"))]
        if done and not partial:
            path = os.path.join(DOWNLOAD_DIR, sorted(done)[0])
            size = os.path.getsize(path)
            if size > 0 and last_size.get(path) == size:
                stable += 1
                if stable >= 2:
                    return path
            else:
                stable = 0
            last_size[path] = size
        time.sleep(1.5)
    return None


# ── Job chính ────────────────────────────────────────────────────────────────
def run_auto_import(on_file, account=None, password=None, use_otp=True, totp_secret=None):
    """
    on_file(path) -> (ok: bool, message: str, count: int): import file lên Supabase.
    """
    global _running, _status, _cancel, _percent, _logs, _result
    _running, _status, _cancel, _percent, _logs, _result = True, "logging_in", False, 0, [], {}

    try:
        d_from, d_to = get_date_range()
        _log("═══════════════════════════════════════")
        _log(f"🚀 Import tự động Tồn Bảo Trì ({d_from:%d/%m/%Y} → {d_to:%d/%m/%Y})")
        _log("═══════════════════════════════════════")

        # ── Bước 1: Đăng nhập ───────────────────────────────────────────
        ok, msg = inside_fpt.login_inside_fpt(account=account, password=password, use_otp=use_otp, totp_secret=totp_secret)
        _log(msg, "info" if ok else "error")
        if not ok:
            _set("error")
            _result.update(success=False, error=msg)
            return
        _set("navigating", 15)
        _check_cancel()

        d = inside_fpt._inside_driver
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        for cmd, args in (
            ("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}),
            ("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": DOWNLOAD_DIR}),
        ):
            try:
                d.execute_cdp_cmd(cmd, args)
            except Exception:
                pass

        # ── Bước 2: Inside New V5 → Hỗ trợ kỹ thuật → Thông tin chung → tab BẢO TRÌ ──
        ok, msg = inside_fpt.navigate_to_bao_tri()
        _log(msg, "info" if ok else "warning")
        _set("filtering", 30)
        _check_cancel()

        # ── Bước 3: Từ ngày / Đến ngày ──────────────────────────────────
        _log(f"Đặt khoảng ngày: {d_from:%d/%m/%Y} → {d_to:%d/%m/%Y}")
        if not _set_date_range(d, d_from, d_to):
            _shot(d, "import_date_fail.png")
            raise RuntimeError("Không đặt được Từ ngày / Đến ngày (xem debug_shots/import_date_fail.png)")
        _check_cancel()

        # ── Bước 4: TỒN POP → Xem báo cáo ───────────────────────────────
        _log("Bấm tab 'TỒN POP'...")
        tab = _find_text(d, ["TỒN POP"], exact=True, timeout=20)
        if tab is None:
            _shot(d, "import_tab_fail.png")
            raise RuntimeError("Không tìm thấy tab 'TỒN POP'")
        _click(d, tab)
        time.sleep(1.5)

        _log("Bấm 'Xem báo cáo'...")
        btn = _find_text(d, ["XEM BÁO CÁO"], exact=False, timeout=20)
        if btn is None:
            _shot(d, "import_view_fail.png")
            raise RuntimeError("Không tìm thấy nút 'Xem báo cáo'")
        _click(d, btn)
        _set("exporting", 50)

        # ── Bước 5: Bấm vào số lượng → Xuất Excel ───────────────────────
        _log("Chờ bảng TỒN POP hiển thị số lượng...")
        link = None
        end = time.time() + 90
        retried_tab = False
        while time.time() < end and link is None:
            _check_cancel()
            time.sleep(2)
            link = _find_number(d)
            if link is None and not retried_tab and time.time() > end - 60:
                retried_tab = True
                t = _find_text(d, ["TỒN POP"], exact=True, timeout=5)
                if t is not None:
                    _click(d, t)
                    b = _find_text(d, ["XEM BÁO CÁO"], exact=False, timeout=5)
                    if b is not None:
                        _click(d, b)
        if link is None:
            _shot(d, "import_count_fail.png")
            raise RuntimeError("Không thấy số lượng Tồn POP (có thể không có dữ liệu)")

        _log(f"Bấm vào số lượng: {link.text.strip()}")
        _click(d, link)
        time.sleep(4)
        _shot(d, "import_after_count.png")
        _check_cancel()

        # Chrome chạy ẩn không hiện được cửa sổ "Save As" của hệ điều hành:
        # thay showSaveFilePicker bằng bản giả để file được tải thẳng vào thư mục downloads.
        d.switch_to.default_content()
        _walk_frames(d, lambda drv: (drv.execute_script(_SAVE_PICKER_STUB_JS), None)[1])
        _check_cancel()

        _log("Bấm 'XUẤT EXCEL'...")
        exp = _find_text(d, ["XUẤT EXCEL"], exact=False, timeout=60)
        if exp is None:
            _shot(d, "import_export_fail.png")
            raise RuntimeError("Không tìm thấy nút 'XUẤT EXCEL'")
        try:
            d.execute_script(_SAVE_PICKER_STUB_JS)  # đảm bảo frame chứa nút cũng đã được thay
        except Exception:
            pass

        before = set(os.listdir(DOWNLOAD_DIR))
        _click(d, exp)

        # Popup xác nhận "Xuất Excel?" → bấm OK
        if _accept_alerts(d, timeout=15):
            _log("Đã bấm OK ở popup 'Xuất Excel?'")
        else:
            _log("Không thấy popup xác nhận 'Xuất Excel?' (bỏ qua)", "warning")
        time.sleep(3)
        try:
            _log(f"Sau khi bấm Xuất Excel: {len(d.window_handles)} cửa sổ, file trong thư mục tải: {sorted(set(os.listdir(DOWNLOAD_DIR)) - before)}")
        except Exception:
            pass
        _shot(d, "import_after_export.png")
        _log("Đang chờ Inside xuất Excel (tối đa 5 phút)...")
        _set("exporting", 70)
        path = _wait_download(before, timeout=300)
        if not path:
            _shot(d, "import_download_fail.png")
            raise RuntimeError("Hết thời gian chờ tải file Excel")
        _log(f"✅ Đã tải file Excel: {os.path.basename(path)}")

        # ── Import lên Supabase ─────────────────────────────────────────
        _set("importing", 85)
        _check_cancel()
        ok, msg, count = on_file(path)
        if not ok:
            raise RuntimeError(f"Import Supabase thất bại: {msg}")
        _log(msg)
        _result.update(success=True, message=msg, count=count, file=os.path.basename(path))
        _set("done", 100)
        _log("═══ Hoàn tất import tự động Tồn Bảo Trì ═══")

    except _Cancelled:
        _log("⛔ Đã dừng import tự động", "warning")
        _set("cancelled")
        _result.update(success=False, error="Đã dừng")
    except Exception as e:
        _log(f"❌ Lỗi import tự động: {e}", "error")
        _set("error")
        _result.update(success=False, error=str(e))
    finally:
        _running = False
        try:
            inside_fpt.close_inside_driver()
        except Exception:
            pass
