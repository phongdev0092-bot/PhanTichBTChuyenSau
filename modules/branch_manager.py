"""
branch_manager.py - Quản lý danh sách Đơn Vị / Chi Nhánh (Branches) cho hệ thống MYBAE AUTO
Lưu trữ danh sách chi nhánh vào file branches.json và ánh xạ bảng PostgreSQL Bảo Trì:
- SG01 / Sài Gòn 1 (mặc định) -> Bảng "bt" trên DEFAULT_DB_URL
- Đơn vị khác [donvi]         -> Tự tạo và gán bảng "bt_[donvi]" trên OTHER_UNIT_DB_URL
"""
import os
import re
import json
import logging
import unicodedata

import config

log = logging.getLogger("branch_manager")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANCHES_FILE = os.path.join(BASE_DIR, "branches.json")
BRANCH_TABLES_FILE = os.path.join(BASE_DIR, "branch_tables.json")

DEFAULT_BRANCHES = [
    "SG01",
    "Sài Gòn 1",
]

_SG01_ALIASES = {
    "", "all", "default",
    "sg01", "sg1", "sg_01", "sg_1",
    "sai gon 1", "sai gon 01", "saigon1", "saigon01", "sai_gon_1", "sai_gon_01",
    "sài gòn 1", "sài gòn 01",
    "chi nhanh mac dinh", "chi nhánh mặc định",
}


def _strip_accents(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFC", str(text).strip())
    s = s.replace("đ", "d").replace("Đ", "D")
    nfd = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")


def is_sg01_branch(branch_name: str | None) -> bool:
    """Kiểm tra xem tên chi nhánh/đơn vị có phải là SG01 (mặc định) hay không."""
    if not branch_name or not str(branch_name).strip():
        return True
    raw_nfc = unicodedata.normalize("NFC", str(branch_name).strip()).lower()
    raw_ascii = _strip_accents(raw_nfc).lower()
    raw_compact = re.sub(r"[\s_\-]+", " ", raw_ascii).strip()
    return (
        raw_nfc in _SG01_ALIASES
        or raw_ascii in _SG01_ALIASES
        or raw_compact in _SG01_ALIASES
    )


def slugify_unit(branch_name: str) -> str:
    """Chuẩn hóa tên đơn vị thành mã hợp lệ cho tên bảng bt_[donvi]."""
    ascii_str = _strip_accents(branch_name).lower()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_str).strip("_")
    return slug or "donvi"


def get_bt_table_name(branch_name: str | None = None) -> str:
    """Trả về tên bảng Bảo Trì ứng với đơn vị: 'bt' cho SG01, 'bt_[donvi]' cho đơn vị khác."""
    if is_sg01_branch(branch_name):
        return getattr(config, "BT_DEFAULT_TABLE", "bt")
    mapping = load_branch_table_map()
    norm_key = unicodedata.normalize("NFC", str(branch_name).strip())
    if norm_key in mapping and mapping[norm_key].get("table_name"):
        return mapping[norm_key]["table_name"]
    slug = slugify_unit(norm_key)
    return slug if slug.startswith("bt_") else f"bt_{slug}"


def get_branch_bt_info(branch_name: str | None = None) -> dict:
    """Trả về thông tin kết nối DB và tên bảng Bảo Trì cho đơn vị."""
    sg01 = is_sg01_branch(branch_name)
    table_name = get_bt_table_name(branch_name)
    db_url = getattr(config, "DEFAULT_DB_URL", "") if sg01 else getattr(config, "OTHER_UNIT_DB_URL", "")
    return {
        "branch": str(branch_name).strip() if branch_name else "SG01",
        "is_sg01": sg01,
        "table_name": table_name,
        "db_url": db_url,
    }


def load_branch_table_map() -> dict:
    if os.path.exists(BRANCH_TABLES_FILE):
        try:
            with open(BRANCH_TABLES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            log.warning(f"Lỗi đọc branch_tables.json: {e}")
    return {}


def save_branch_table_map(mapping: dict) -> bool:
    try:
        with open(BRANCH_TABLES_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"Lỗi ghi branch_tables.json: {e}")
        return False


def load_branches() -> list[str]:
    """Đọc danh sách chi nhánh từ file branches.json. Nếu chưa có thì khởi tạo."""
    branches = []
    if os.path.exists(BRANCHES_FILE):
        try:
            with open(BRANCHES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    branches = [unicodedata.normalize("NFC", str(b).strip()) for b in data if str(b).strip()]
        except Exception as e:
            log.error(f"Lỗi đọc file branches.json: {e}")

    if not branches:
        branches = list(DEFAULT_BRANCHES)
        save_branches(branches)

    # Đảm bảo SG01 luôn có mặt ở đầu danh sách
    unique = []
    seen = set()
    for b in ["SG01"] + branches:
        key = b.lower()
        if key not in seen:
            seen.add(key)
            unique.append(b)
    return unique


def save_branches(branches: list[str]) -> bool:
    """Ghi danh sách chi nhánh vào file branches.json."""
    unique = []
    seen = set()
    for b in branches:
        s = unicodedata.normalize("NFC", str(b).strip())
        if s and s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)
    try:
        with open(BRANCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(unique, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"Lỗi ghi file branches.json: {e}")
        return False


def add_branch(branch_name: str) -> tuple[bool, str, list[str]]:
    """
    Thêm đơn vị/chi nhánh mới vào danh sách.
    Nếu là đơn vị khác SG01 -> tự động khởi tạo bảng bt_[donvi] trên PostgreSQL DB đơn vị mới
    và gán vào link table cho đơn vị đó.
    """
    branch_name = unicodedata.normalize("NFC", (branch_name or "").strip())
    if not branch_name:
        return False, "Tên đơn vị/chi nhánh không được để trống", load_branches()

    branches = load_branches()
    already_exists = any(existing.lower() == branch_name.lower() for existing in branches)
    if not already_exists:
        branches.append(branch_name)
        save_branches(branches)

    info = get_branch_bt_info(branch_name)
    tbl = info["table_name"]

    # Lưu mapping vào branch_tables.json
    mapping = load_branch_table_map()
    mapping[branch_name] = {
        "table_name": tbl,
        "db_url": info["db_url"],
        "is_sg01": info["is_sg01"],
    }
    save_branch_table_map(mapping)

    # Nếu không phải SG01 thì tự động tạo bảng bt_[donvi] trên PostgreSQL
    if not info["is_sg01"]:
        try:
            from modules import supabase_db
            ok_tbl, msg_tbl = supabase_db.ensure_bt_unit_table(branch_name)
            if not ok_tbl:
                log.warning(f"Cảnh báo tạo bảng {tbl}: {msg_tbl}")
                return True, f"Đã lưu đơn vị '{branch_name}' (gán bảng '{tbl}', lưu ý DB: {msg_tbl})", load_branches()
            return True, f"✅ Đã khởi tạo đơn vị '{branch_name}' và tự động tạo bảng '{tbl}' thành công!", load_branches()
        except Exception as e:
            log.error(f"Lỗi khởi tạo bảng cho đơn vị {branch_name}: {e}")

    return True, f"✅ Đã chọn đơn vị '{branch_name}' (bảng '{tbl}')", load_branches()


def delete_branch(branch_name: str) -> tuple[bool, str, list[str]]:
    """Xóa chi nhánh khỏi danh sách (không xóa dữ liệu đã lưu trên DB)."""
    branch_name = unicodedata.normalize("NFC", (branch_name or "").strip())
    branches = load_branches()
    new_list = [b for b in branches if b.lower() != branch_name.lower()]
    if len(new_list) == len(branches):
        return False, f"Không tìm thấy chi nhánh '{branch_name}'", branches

    if not new_list:
        new_list = list(DEFAULT_BRANCHES)

    save_branches(new_list)
    return True, f"Đã xóa chi nhánh '{branch_name}' khỏi danh sách", load_branches()
