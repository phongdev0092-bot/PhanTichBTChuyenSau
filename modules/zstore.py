"""
zstore.py – Lưu/đọc JSON nén bằng zstandard (file `<path>.zst`).

- Tự động đọc file .json cũ nếu chưa có .zst (di chuyển dữ liệu liền mạch).
- Nếu chưa cài `zstandard` thì rơi về ghi JSON thường, không lỗi.
- Ghi qua file tạm rồi thay thế để không hỏng dữ liệu nếu app bị tắt giữa chừng.
"""
import os
import json
import logging

log = logging.getLogger("zstore")

try:
    import zstandard as _zstd
except Exception:  # chưa cài
    _zstd = None

LEVEL = 10


def _zst_path(path: str) -> str:
    return path + ".zst"


def load_json(path: str, default=None):
    """Đọc dữ liệu: ưu tiên path.zst, sau đó path (JSON thường)."""
    zp = _zst_path(path)
    try:
        if os.path.exists(zp):
            if _zstd is None:
                log.error("Có file .zst nhưng chưa cài zstandard (pip install zstandard)")
                return default
            with open(zp, "rb") as f:
                raw = _zstd.ZstdDecompressor().stream_reader(f).read()
            return json.loads(raw.decode("utf-8"))
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Lỗi đọc {path}: {e}")
    return default


def save_json(path: str, data) -> None:
    """Ghi dữ liệu nén (hoặc JSON thường nếu thiếu zstandard). Xóa bản cũ không còn dùng."""
    if _zstd is None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return
    zp = _zst_path(path)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = zp + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_zstd.ZstdCompressor(level=LEVEL).compress(raw))
    os.replace(tmp, zp)
    if os.path.exists(path):  # đã di chuyển sang .zst
        try:
            os.remove(path)
        except OSError:
            pass


def remove(path: str) -> None:
    for p in (path, _zst_path(path)):
        if os.path.exists(p):
            os.remove(p)
