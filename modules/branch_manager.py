"""
branch_manager.py - Quan ly danh sach Chi Nhanh (Branches) cho he thong MYBAE AUTO
Luu tru danh sach chi nhanh vao file branches.json va dong bo voi Supabase.
"""
import os
import json
import logging

log = logging.getLogger("branch_manager")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANCHES_FILE = os.path.join(BASE_DIR, "branches.json")

DEFAULT_BRANCHES = [
    "Chi Nhánh Mặc Định",
]

def load_branches() -> list[str]:
    """Doc danh sach chi nhanh tu file branches.json. Neu chua co thi khoi tao."""
    branches = []
    if os.path.exists(BRANCHES_FILE):
        try:
            with open(BRANCHES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    branches = [str(b).strip() for b in data if str(b).strip()]
        except Exception as e:
            log.error(f"Loi doc file branches.json: {e}")

    if not branches:
        branches = list(DEFAULT_BRANCHES)
        save_branches(branches)

    return sorted(list(set(branches)))


def save_branches(branches: list[str]) -> bool:
    """Ghi danh sach chi nhanh vao file branches.json."""
    unique_branches = sorted(list(set(str(b).strip() for b in branches if str(b).strip())))
    try:
        with open(BRANCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(unique_branches, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"Loi ghi file branches.json: {e}")
        return False


def add_branch(branch_name: str) -> tuple[bool, str, list[str]]:
    """Them chi nhanh moi vao danh sach."""
    branch_name = (branch_name or "").strip()
    if not branch_name:
        return False, "Ten chi nhanh khong duoc de trong", load_branches()

    branches = load_branches()
    for existing in branches:
        if existing.lower() == branch_name.lower():
            return True, f"Chi nhanh '{existing}' da ton tai", branches

    branches.append(branch_name)
    save_branches(branches)
    return True, f"Da them chi nhanh '{branch_name}' thanh cong", load_branches()


def delete_branch(branch_name: str) -> tuple[bool, str, list[str]]:
    """Xoa chi nhanh khoi danh sach (khong xoa du lieu da luu tren Supabase)."""
    branch_name = (branch_name or "").strip()
    branches = load_branches()
    new_list = [b for b in branches if b.lower() != branch_name.lower()]
    if len(new_list) == len(branches):
        return False, f"Khong tim thay chi nhanh '{branch_name}'", branches

    if not new_list:
        new_list = list(DEFAULT_BRANCHES)

    save_branches(new_list)
    return True, f"Da xoa chi nhanh '{branch_name}' khoi danh sach", new_list
