"""
captain_manager.py - Quản lý cấu hình MAP Đội Trưởng - Nhân Viên Kỹ Thuật
Hỗ trợ lưu trữ bền vững tại team_captains.json, nạp từ Excel/CSV, đồng bộ theo Chi Nhánh.
"""
import os
import json
import io
import logging
import pandas as pd

log = logging.getLogger("captain_manager")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTAINS_FILE = os.path.join(BASE_DIR, "team_captains.json")


def _normalize_branch_key(branch: str = None) -> str:
    if not branch or str(branch).strip().lower() in ("all", "tất cả", "tat ca", "", "none"):
        return "default"
    return str(branch).strip()


def load_data() -> dict:
    """Đọc toàn bộ dữ liệu từ team_captains.json."""
    if not os.path.exists(CAPTAINS_FILE):
        return {"branches": {}, "default": {}}

    try:
        with open(CAPTAINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"branches": {}, "default": {}}
            if "branches" not in data:
                data["branches"] = {}
            if "default" not in data:
                data["default"] = {}
            return data
    except Exception as e:
        log.error(f"Lỗi đọc file team_captains.json: {e}")
        return {"branches": {}, "default": {}}


def save_data(data: dict) -> bool:
    """Ghi toàn bộ cấu hình vào team_captains.json."""
    try:
        with open(CAPTAINS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"Lỗi ghi file team_captains.json: {e}")
        return False


def get_captains(branch: str = None) -> dict:
    """
    Lấy danh sách Đội Trưởng và Nhân Viên theo chi nhánh.
    Trả về dict: {
        "captains_map": { "Đội Trưởng A": ["NV1", "NV2"] },
        "sorted_captains": ["Đội Trưởng A", ...],
        "emp_to_captain": { "NV1": "Đội Trưởng A", ... },
        "branch": branch
    }
    """
    data = load_data()
    branches_map = data.get("branches", {})
    default_map = data.get("default", {})

    b_key = _normalize_branch_key(branch)
    captains_map = {}

    if b_key != "default":
        # Tìm chính xác hoặc case-insensitive theo tên chi nhánh
        for k, v in branches_map.items():
            if k.strip().lower() == b_key.lower():
                captains_map = dict(v)
                break

    # Nếu chi nhánh cụ thể chưa có cấu hình riêng, kiểm tra default
    if not captains_map and default_map:
        captains_map = dict(default_map)

    # Nếu default cũng trống nhưng trong branches chỉ có đúng 1 chi nhánh có data, dùng luôn
    if not captains_map and len(branches_map) == 1:
        only_b = list(branches_map.values())[0]
        if only_b:
            captains_map = dict(only_b)

    # Xây dựng bảng tra cứu emp -> captain
    emp_to_captain = {}
    cleaned_map = {}
    for cap, emps in captains_map.items():
        cap_name = str(cap).strip()
        if not cap_name:
            continue
        cleaned_emps = []
        for e in emps:
            e_str = str(e).strip().upper()
            if e_str and e_str not in ("NAN", "NONE", ""):
                cleaned_emps.append(e_str)
                emp_to_captain[e_str] = cap_name
        cleaned_map[cap_name] = sorted(list(set(cleaned_emps)))

    return {
        "captains_map": cleaned_map,
        "sorted_captains": sorted(cleaned_map.keys()),
        "emp_to_captain": emp_to_captain,
        "branch": branch or "default"
    }


def save_captains(captains_map: dict, branch: str = None) -> tuple[bool, str]:
    """Lưu cấu hình captains_map cho chi nhánh chỉ định."""
    data = load_data()
    b_key = _normalize_branch_key(branch)

    # Làm sạch dữ liệu trước khi lưu
    cleaned = {}
    for cap, emps in (captains_map or {}).items():
        c_name = str(cap).strip()
        if not c_name:
            continue
        clean_list = []
        for e in emps:
            e_code = str(e).strip().upper()
            if e_code and e_code not in ("NAN", "NONE", ""):
                if e_code not in clean_list:
                    clean_list.append(e_code)
        cleaned[c_name] = clean_list

    if b_key == "default":
        data["default"] = cleaned
    else:
        # Tìm key gốc nếu có để giữ nguyên hoa thường
        found_key = b_key
        for k in data.get("branches", {}):
            if k.strip().lower() == b_key.lower():
                found_key = k
                break
        data["branches"][found_key] = cleaned

        # Nếu default chưa có gì, gán luôn làm default để tiện lợi
        if not data.get("default"):
            data["default"] = cleaned

    ok = save_data(data)
    if ok:
        total_nv = sum(len(v) for v in cleaned.values())
        return True, f"Đã lưu thành công {len(cleaned)} Đội Trưởng ({total_nv} Nhân Viên) cho chi nhánh [{branch or 'Mặc định'}]."
    return False, "Không thể ghi dữ liệu cấu hình vào file."


def import_from_dataframe(df: pd.DataFrame, branch: str = None) -> tuple[bool, str, dict]:
    """
    Tự động đọc DataFrame từ file Excel/CSV tải lên để sinh MAP Đội Trưởng -> Nhân Viên.
    Hỗ trợ nhận diện thông minh các tiêu đề cột.
    """
    if df is None or df.empty:
        return False, "File dữ liệu rỗng hoặc không đọc được", {}

    cols = [str(c).strip() for c in df.columns]

    # Tìm cột Đội Trưởng
    captain_col = None
    for c in cols:
        c_low = c.lower()
        if any(k in c_low for k in ["đội trưởng", "doi truong", "đội", "captain", "leader", "tổ trưởng", "to truong", "nhóm trưởng", "team"]):
            captain_col = c
            break

    # Tìm cột Nhân Viên
    emp_col = None
    for c in cols:
        c_low = c.lower()
        if any(k in c_low for k in ["nhân viên", "nhan vien", "account", "inside_account", "ktv", "kỹ thuật", "họ tên", "ho ten", "nv", "user"]):
            emp_col = c
            break

    # Nếu không tìm thấy theo tên cột, dùng cột 0 và cột 1 nếu có >= 2 cột
    if not captain_col or not emp_col:
        if len(cols) >= 2:
            captain_col = cols[0]
            emp_col = cols[1]
        else:
            return False, f"File cần ít nhất 2 cột (Cột Đội Trưởng và Cột Nhân Viên). Các cột hiện có: {', '.join(cols)}", {}

    captains_map = {}
    rows = df.fillna("").to_dict(orient="records")

    for r in rows:
        cap_val = str(r.get(captain_col, "")).strip()
        emp_val = str(r.get(emp_col, "")).strip().upper()

        if not cap_val or cap_val.lower() in ("nan", "none", "", "đội trưởng", "doi truong"):
            continue
        if not emp_val or emp_val.lower() in ("nan", "none", "", "nhân viên", "nhan vien"):
            continue

        if cap_val not in captains_map:
            captains_map[cap_val] = []
        if emp_val not in captains_map[cap_val]:
            captains_map[cap_val].append(emp_val)

    if not captains_map:
        return False, "Không trích xuất được bản ghi hợp lệ nào từ file.", {}

    # Lưu lại
    save_ok, msg = save_captains(captains_map, branch=branch)
    total_nv = sum(len(v) for v in captains_map.values())
    if save_ok:
        return True, f"✅ Nạp thành công {len(captains_map)} Đội Trưởng và {total_nv} Nhân Viên từ file.", captains_map
    return False, msg, captains_map


def generate_template_excel() -> bytes:
    """Tạo file mẫu Excel (.xlsx) để người dùng tải về điền dữ liệu."""
    sample_data = [
        {"Đội Trưởng": "Đội Trưởng 1 - Nguyễn Văn A", "Nhân Viên (Account Inside)": "PNC01.ANHNH9"},
        {"Đội Trưởng 1 - Nguyễn Văn A": "Đội Trưởng 1 - Nguyễn Văn A", "Nhân Viên (Account Inside)": "PNC01.ANHV"},
        {"Đội Trưởng 1 - Nguyễn Văn A": "Đội Trưởng 1 - Nguyễn Văn A", "Nhân Viên (Account Inside)": "PNC01.BALT"},
        {"Đội Trưởng 2 - Trần Văn B": "Đội Trưởng 2 - Trần Văn B", "Nhân Viên (Account Inside)": "PNC01.BAOMQ1"},
        {"Đội Trưởng 2 - Trần Văn B": "Đội Trưởng 2 - Trần Văn B", "Nhân Viên (Account Inside)": "PNC01.BINHPT2"},
    ]
    # Dùng DataFrame đơn giản chuẩn
    df = pd.DataFrame([
        {"Đội Trưởng": "Đội Trưởng Nguyễn Văn A", "Nhân Viên (Account Inside)": "PNC01.ANHNH9"},
        {"Đội Trưởng": "Đội Trưởng Nguyễn Văn A", "Nhân Viên (Account Inside)": "PNC01.ANHV"},
        {"Đội Trưởng": "Đội Trưởng Trần Văn B", "Nhân Viên (Account Inside)": "PNC01.BALT"},
        {"Đội Trưởng": "Đội Trưởng Trần Văn B", "Nhân Viên (Account Inside)": "PNC01.BAOMQ1"}
    ])

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="MAP_Doi_Truong")
    out.seek(0)
    return out.read()


def export_excel(branch: str = None) -> bytes:
    """Xuất danh sách MAP Đội Trưởng hiện tại ra file Excel."""
    res = get_captains(branch=branch)
    cap_map = res.get("captains_map", {})

    rows = []
    for cap, emps in cap_map.items():
        if not emps:
            rows.append({"Đội Trưởng": cap, "Nhân Viên (Account Inside)": ""})
        else:
            for e in emps:
                rows.append({"Đội Trưởng": cap, "Nhân Viên (Account Inside)": e})

    if not rows:
        rows = [{"Đội Trưởng": "Chưa có Đội Trưởng", "Nhân Viên (Account Inside)": ""}]

    df = pd.DataFrame(rows)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="MAP_Doi_Truong")
    out.seek(0)
    return out.read()
