"""
supabase_db.py - Quản lý truy vấn và Import dữ liệu từ Supabase REST API
Cấu hình 2 bảng chính:
1. bao_tri (hoặc contracts) - Hợp đồng Bảo Trì
2. ton_bao_tri - Hợp đồng Tồn Bảo Trì
"""
import logging
import time
from datetime import datetime, timedelta
import requests
import pandas as pd
import sys
import os

import config

log = logging.getLogger("supabase_db")
CACHE_TTL = 300

_cache_store = {}
_cache_times = {}

ALLOWED_TABLES = ("bao_tri", "ton_bao_tri", "contracts", "employees", "cll30", "kh_cls")

def get_supabase_key() -> str:
    return getattr(config, "SUPABASE_KEY", "sb_publishable_Tgnpn53W9b0dxDSYCkNXOw_jug0G3ML")

def update_supabase_key(new_key: str) -> tuple[bool, str]:
    new_key = new_key.strip()
    if not new_key:
        return False, "Khóa Supabase Key không được rỗng"

    config.SUPABASE_KEY = new_key
    _cache_store.clear()
    _cache_times.clear()

    try:
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            import re
            if "SUPABASE_KEY =" in content:
                content = re.sub(r'SUPABASE_KEY\s*=\s*[\'"].*?[\'"]', f"SUPABASE_KEY = '{new_key}'", content)
            else:
                content += f"\nSUPABASE_KEY = '{new_key}'\n"
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:
        log.error(f"Lỗi lưu config.py: {e}")

    return True, "✅ Đã lưu Supabase Key thành công!"

def get_headers():
    key = get_supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def _make_request(method, endpoint, json_data=None, params=None, timeout=20):
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{endpoint}"
    headers = get_headers()
    try:
        resp = requests.request(method, url, headers=headers, json=json_data, params=params, timeout=timeout)
        return resp
    except Exception as e:
        log.error(f"Lỗi Supabase Request [{method} {endpoint}]: {e}")
        return None


def fetch_all_records(table_name: str, select: str = "*", limit: int = 50000) -> list[dict]:
    """
    Lấy toàn bộ dữ liệu từ bảng Supabase.
    Sử dụng Range pagination (0-999, 1000-1999...) để vượt qua giới hạn 1000 bản ghi/lần của PostgREST.
    Có cache bộ nhớ 120s để tối ưu tốc độ.
    """
    global _cache_store, _cache_times
    now = time.time()
    cache_key = f"records_{table_name}_{select}_{limit}"
    if cache_key in _cache_store and (now - _cache_times.get(cache_key, 0)) < 120:
        return _cache_store[cache_key]

    headers = get_headers()
    base_url = config.SUPABASE_URL.rstrip('/')
    url = f"{base_url}/rest/v1/{table_name}?select={select}"

    all_records = []
    page = 0
    page_size = 1000

    while len(all_records) < limit:
        h = headers.copy()
        start = page * page_size
        end = min((page + 1) * page_size - 1, limit - 1)
        h["Range"] = f"{start}-{end}"

        try:
            r = requests.get(url, headers=h, timeout=25)
            if r.status_code in (200, 206):
                data = r.json()
                if not data or not isinstance(data, list):
                    break
                all_records.extend(data)
                if len(data) < page_size:
                    break
                page += 1
            elif r.status_code == 404:
                # Fallback alias giữa bao_tri và contracts
                fallback = "contracts" if table_name == "bao_tri" else ("bao_tri" if table_name == "contracts" else None)
                if fallback:
                    return fetch_all_records(fallback, select=select, limit=limit)
                break
            else:
                log.error(f"Lỗi fetch_all_records({table_name}) page {page}: HTTP {r.status_code}")
                break
        except Exception as e:
            log.error(f"Ngoại lệ fetch_all_records({table_name}): {e}")
            break

    if all_records:
        _cache_store[cache_key] = all_records
        _cache_times[cache_key] = now
        log.info(f"fetch_all_records({table_name}): Đã nạp thành công {len(all_records)} bản ghi từ Supabase")

    return all_records


def fetch_table_summary() -> dict:
    """Trả về số lượng bản ghi của 2 bảng chính: bao_tri và ton_bao_tri."""
    tables = ["bao_tri", "ton_bao_tri"]
    summary = {}
    headers = get_headers()
    headers["Prefer"] = "count=exact"
    headers["Range"] = "0-0"
    
    for tbl in tables:
        url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{tbl}?select=id"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code in (200, 206):
                content_range = r.headers.get("Content-Range", "")
                if "/" in content_range:
                    total = content_range.split("/")[-1]
                    summary[tbl] = int(total) if total.isdigit() else len(r.json())
                else:
                    summary[tbl] = len(r.json())
            else:
                # Trử fallback nếu chưa tạo tên bảng bao_tri
                if tbl == "bao_tri":
                    r_fb = requests.get(f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/contracts?select=id", headers=headers, timeout=10)
                    if r_fb.status_code in (200, 206):
                        cr = r_fb.headers.get("Content-Range", "")
                        summary[tbl] = int(cr.split("/")[-1]) if "/" in cr and cr.split("/")[-1].isdigit() else len(r_fb.json())
                    else:
                        summary[tbl] = 0
        except Exception as e:
            log.error(f"Lỗi đếm số lượng {tbl}: {e}")
            summary[tbl] = 0
            
    return summary


def extract_row_fields(r: dict, table: str = "bao_tri") -> tuple[str, str, str, str]:
    """
    Trích xuất (so_hd, nhan_vien, tg_hoan_tat, tg_tao) chuẩn xác từ bản ghi r (và r['data']).
    
    Quy tắc Tồn Bảo Trì (ton_bao_tri):
    - Cột F (index 5) là SỐ HD (mã hợp đồng)
    - Cột H (index 7) là THỜI GIAN TẠO (dùng cho lọc datepicker)
    - Cột S (index 18) là NHÂN VIÊN / NHÂN SỰ
    
    Quy tắc Bảo Trì (bao_tri):
    - Quét các cột contract code, không lấy nhầm 'Loại hợp đồng', 'Trạng thái'...
    - Cột ngày lọc là TG HOÀN TẤT
    """
    if not isinstance(r, dict):
        return "", "", ""

    row_data = r.get("data") if isinstance(r.get("data"), dict) else r
    if not isinstance(row_data, dict):
        row_data = {}

    row_vals = list(row_data.values())
    so_hd = ""
    nhan_vien = ""
    tg_hoan_tat = ""
    tg_tao = ""  # Cột TG Tạo riêng cho ton_bao_tri

    is_ton = (table == "ton_bao_tri")

    # 1. SỐ HỢP ĐỒNG (so_hd)
    if is_ton:
        for cand in ["F", "Số HĐ", "SỐ HĐ", "Số HD", "SỐ HD", "Mã HĐ", "MÃ HĐ", "SoHD", "so_hd", "Số Hợp Đồng"]:
            if cand in row_data:
                v = str(row_data[cand]).strip()
                if v and v.upper() not in ("NAN", "NONE", ""):
                    so_hd = v
                    break
        if not so_hd and len(row_vals) > 5:
            v = str(row_vals[5]).strip()
            if v and v.upper() not in ("NAN", "NONE", ""):
                so_hd = v

    if not so_hd:
        top_hd = str(r.get("so_hd") or "").strip()
        if top_hd and top_hd.upper() not in ("NAN", "NONE", ""):
            top_hd_low = top_hd.lower()
            if not any(x in top_hd_low for x in ("loại", "bảo trì ftth", "trạng thái", "dịch vụ", "nội dung")):
                so_hd = top_hd

    if not so_hd:
        for cand in ["Số HĐ", "SỐ HĐ", "Số HD", "SỐ HD", "Mã HĐ", "MÃ HĐ", "SoHD", "so_hd", "Contract", "Hợp đồng", "hop_dong", "Số Hợp Đồng"]:
            if cand in row_data:
                cand_low = str(cand).lower()
                if not any(x in cand_low for x in ("loại", "trạng thái", "dịch vụ", "nội dung")):
                    v = str(row_data[cand]).strip()
                    if v and v.upper() not in ("NAN", "NONE", ""):
                        so_hd = v
                        break

    if not so_hd:
        for k, v in row_data.items():
            kl = str(k).lower()
            if any(x in kl for x in ("hợp đồng", "so_hd", "contract", "số hđ", "mã hđ")) and not any(x in kl for x in ("loại", "trạng thái", "dịch vụ", "nội dung")):
                val = str(v).strip()
                if val and val.upper() not in ("NAN", "NONE", ""):
                    so_hd = val
                    break

    # 2. THỜI GIAN
    # ton_bao_tri: TG TẠO (cột H index 7) → dùng cho datepicker lọc
    # bao_tri: TG HOÀN TẤT → dùng cho datepicker lọc
    if is_ton:
        # TG TẠO cho ton_bao_tri
        for cand in ["H", "TG tạo", "Thời gian tạo", "THỜI GIAN TẠO", "tg_tao", "Ngày tạo", "NGÀY TẠO", "Thời gian", "THỜI GIAN", "Ngày", "NGÀY"]:
            if cand in row_data:
                v = str(row_data[cand]).strip()
                if v and v.upper() not in ("NAN", "NONE", ""):
                    tg_tao = v
                    break
        if not tg_tao and len(row_vals) > 7:
            v = str(row_vals[7]).strip()
            if v and v.upper() not in ("NAN", "NONE", ""):
                tg_tao = v
        # tg_hoan_tat cho ton cũng gán bằng tg_tao (để tương thích)
        tg_hoan_tat = tg_tao
    else:
        # TG HOÀN TẤT cho bao_tri
        top_tg = str(r.get("tg_hoan_tat") or r.get("ngay") or "").strip()
        if top_tg and top_tg.upper() not in ("NAN", "NONE", ""):
            tg_hoan_tat = top_tg

        if not tg_hoan_tat:
            for cand in ["tg_hoan_tat", "TG hoàn tất", "TG Hoàn Tất", "Thời gian hoàn tất", "THỜI GIAN HOÀN TẤT",
                         "Ngày hoàn tất", "NGÀY HOÀN TẤT", "Thời gian", "THỜI GIAN", "Ngày", "ngay", "date"]:
                if cand in row_data:
                    v = str(row_data[cand]).strip()
                    if v and v.upper() not in ("NAN", "NONE", ""):
                        tg_hoan_tat = v
                        break

        if not tg_hoan_tat:
            for k, v in row_data.items():
                kl = str(k).lower()
                if any(x in kl for x in ("hoàn tất", "tg_hoan_tat", "thời gian", "ngày", "date")):
                    val = str(v).strip()
                    if val and val.upper() not in ("NAN", "NONE", ""):
                        tg_hoan_tat = val
                        break
        tg_tao = tg_hoan_tat  # bao_tri không có TG Tạo riêng

    # 3. NHÂN VIÊN (nhan_vien)
    if is_ton:
        for cand in ["S", "Nhân sự", "NHÂN SỰ", "Nhân viên", "NHÂN VIÊN", "nhan_vien", "Account", "inside_account"]:
            if cand in row_data:
                v = str(row_data[cand]).strip()
                if v and v.upper() not in ("NAN", "NONE", ""):
                    nhan_vien = v
                    break
        if not nhan_vien and len(row_vals) > 18:
            v = str(row_vals[18]).strip()
            if v and v.upper() not in ("NAN", "NONE", ""):
                nhan_vien = v

    if not nhan_vien:
        top_nv = str(r.get("nhan_vien") or "").strip()
        if top_nv and top_nv.upper() not in ("NAN", "NONE", ""):
            nhan_vien = top_nv

    if not nhan_vien:
        for cand in ["Nhân sự", "nhan_su", "Nhân viên", "nhan_vien", "inside_account", "Account", "NhanVien", "Employee"]:
            if cand in row_data:
                v = str(row_data[cand]).strip()
                if v and v.upper() not in ("NAN", "NONE", ""):
                    nhan_vien = v
                    break

    if not nhan_vien:
        for k, v in row_data.items():
            kl = str(k).lower()
            if any(x in kl for x in ("nhân sự", "nhan_su", "nhân viên", "nhan_vien", "account")):
                val = str(v).strip()
                if val and val.upper() not in ("NAN", "NONE", ""):
                    nhan_vien = val
                    break

    return so_hd, nhan_vien, tg_hoan_tat, tg_tao


def get_employees(force: bool = False, table: str = None) -> list[str]:
    """
    Lấy danh sách nhân viên.
    table=None          → gộp cả 2 bảng
    table='bao_tri'     → chỉ bảo trì
    table='ton_bao_tri' → chỉ tồn bảo trì
    """
    global _cache_store, _cache_times
    now = time.time()
    cache_key = f"employees_{table or 'all'}"
    if not force and cache_key in _cache_store and (now - _cache_times.get(cache_key, 0)) < CACHE_TTL:
        return _cache_store[cache_key]

    employees = set()
    tables_to_query = [table] if table in ("bao_tri", "ton_bao_tri") else ["bao_tri", "ton_bao_tri"]

    for tbl in tables_to_query:
        recs = fetch_all_records(tbl)
        for r in recs:
            _, nv, _, _ = extract_row_fields(r, table=tbl)
            if nv and nv.upper() not in ("NAN", "NONE", ""):
                employees.add(nv)

    if not table:
        recs_emp = fetch_all_records("employees", select="inside_account,ho_ten")
        for r in recs_emp:
            nv = str(r.get("inside_account") or r.get("ho_ten") or "").strip()
            if nv and nv.upper() not in ("NAN", "NONE", ""):
                employees.add(nv)

    result = sorted(list(employees))
    _cache_store[cache_key] = result
    _cache_times[cache_key] = now
    log.info(f"get_employees(table={table}): {len(result)} nhân viên")
    return result


def get_contracts(selected_employees: list, start_date=None, end_date=None,
                  lookback_days: int = 2, table: str = "bao_tri") -> list[dict]:
    """
    Truy vấn danh sách hợp đồng từ bảng chỉ định (bao_tri hoặc ton_bao_tri).
    Lọc chính xác theo tg_hoan_tat và ngày người dùng đã chọn.
    """
    if not selected_employees:
        return []

    if table not in ("bao_tri", "ton_bao_tri"):
        table = "bao_tri"

    records = fetch_all_records(table)
    if not records:
        log.warning(f"Bảng {table} trên Supabase rỗng.")
        return []

    unwrapped_list = []
    for r in records:
        so_hd, nhan_vien, tg_hoan_tat, tg_tao = extract_row_fields(r, table=table)
        row_data = r.get("data") if isinstance(r.get("data"), dict) else r

        # Dùng cột ngày phù hợp với từng bảng để lọc:
        # bao_tri → tg_hoan_tat (TG Hoàn Tất)
        # ton_bao_tri → tg_tao (TG Tạo, cột H)
        date_for_filter = tg_tao if table == "ton_bao_tri" else tg_hoan_tat

        unwrapped_list.append({
            "so_hd": str(so_hd or "").strip(),
            "nhan_vien": str(nhan_vien or "").strip(),
            "tg_hoan_tat": str(tg_hoan_tat or "").strip(),
            "tg_tao": str(tg_tao or "").strip(),
            "date_for_filter": str(date_for_filter or "").strip(),
            "raw_row": row_data
        })

    df = pd.DataFrame(unwrapped_list)
    if df.empty or "nhan_vien" not in df.columns:
        return []

    # 2. Ép kiểu ngày chính xác theo đúng cột lọc của từng bảng:
    # bao_tri → TG Hoàn Tất; ton_bao_tri → TG Tạo
    date_col = "date_for_filter" if "date_for_filter" in df.columns else "tg_hoan_tat"
    parsed_dates = pd.to_datetime(df[date_col], errors="coerce", format="mixed", dayfirst=True)
    df["_datetime"] = parsed_dates
    df["_date"] = parsed_dates.dt.date

    # 3. Lọc theo khoảng ngày người dùng chọn
    if start_date or end_date:
        start_dt = None
        end_dt = None
        if start_date:
            try:
                start_dt = datetime.strptime(str(start_date).strip(), "%Y-%m-%d").date()
            except Exception:
                pass
        if end_date:
            try:
                end_dt = datetime.strptime(str(end_date).strip(), "%Y-%m-%d").date()
            except Exception:
                pass

        if start_dt and end_dt:
            if start_dt > end_dt:
                start_dt, end_dt = end_dt, start_dt
            mask_date = (df["_date"] >= start_dt) & (df["_date"] <= end_dt)
        elif start_dt:
            mask_date = (df["_date"] >= start_dt)
        elif end_dt:
            mask_date = (df["_date"] <= end_dt)
        else:
            mask_date = pd.Series(True, index=df.index)
    else:
        # Nếu người dùng xóa lọc ngày (để trống) -> Lấy tất cả theo nhân viên đã chọn
        mask_date = pd.Series(True, index=df.index)

    nv_col = df["nhan_vien"].fillna("").astype(str).str.strip().str.upper()
    selected_stripped = [str(e).strip().upper() for e in selected_employees if str(e).strip()]
    mask_nv = nv_col.isin(selected_stripped)

    # Lọc kết hợp CẢ Nhân viên VÀ Ngày
    filtered = df[mask_date & mask_nv].copy()

    # SẮP XẾP (SORT) kết quả theo thời gian mới nhất lên đầu
    if not filtered.empty and "_datetime" in filtered.columns:
        filtered = filtered.sort_values(by="_datetime", ascending=False, na_position="last")

    results = []
    seen = set()
    for _, row in filtered.iterrows():
        so_hd = str(row.get("so_hd")).strip()
        if not so_hd or so_hd in ("nan", "None") or so_hd in seen:
            continue
        seen.add(so_hd)
        results.append({
            "so_hd": so_hd,
            "nhan_vien": str(row.get("nhan_vien")).strip(),
            "tg_hoan_tat": str(row.get("tg_hoan_tat")).strip(),
            "tg_tao": str(row.get("tg_tao")).strip(),
            "ngay": str(row.get("_date") or ""),
            "data_source": table,
        })

    date_field_used = "TG Tạo" if table == "ton_bao_tri" else "TG Hoàn Tất"
    log.info(f"Supabase ({table}): Lọc theo [{date_field_used}] – Đã lọc chính xác {len(results)} hợp đồng cho nhân viên {selected_employees} từ ngày {start_date} đến {end_date}")
    return results



def get_team_captains(force: bool = False) -> dict:
    """Lấy danh sách Đội Trưởng từ các bảng."""
    global _cache_store, _cache_times
    now = time.time()
    if not force and "captains" in _cache_store and (now - _cache_times.get("captains", 0)) < CACHE_TTL:
        return _cache_store["captains"]

    records = fetch_all_records("employees") or fetch_all_records("bao_tri")
    captains_map = {}
    emp_to_captain = {}

    for row in records:
        account = str(row.get("inside_account") or row.get("nhan_vien") or row.get("ho_ten") or "").strip().upper()
        captain = str(row.get("doi_truong") or "").strip()
        if account and account not in ("NAN", "NONE", ""):
            if captain and captain not in ("nan", "None", ""):
                if captain not in captains_map:
                    captains_map[captain] = []
                captains_map[captain].append(account)
                emp_to_captain[account] = captain

    res = {
        "captains_map": captains_map,
        "sorted_captains": sorted(captains_map.keys()),
        "emp_to_captain": emp_to_captain
    }
    _cache_store["captains"] = res
    _cache_times["captains"] = now
    return res


def get_cll30_analytics(start_date=None, end_date=None, top_n=10, selected_captain=None) -> dict:
    """
    Tính Tỷ lệ Tồn Bảo Trì dựa trên bảng ton_bao_tri và bao_tri trên Supabase.
    """
    try:
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
        else:
            end_dt = datetime.now().date()
    except Exception:
        end_dt = datetime.now().date()

    month_start_dt = end_dt.replace(day=1)

    # 1. Đọc dữ liệu bảng ton_bao_tri
    ton_records = fetch_all_records("ton_bao_tri")
    ton_counts = {}
    if ton_records:
        unwrapped_ton = []
        for r in ton_records:
            _, nv, tg_hoan, tg_tao = extract_row_fields(r, table="ton_bao_tri")
            unwrapped_ton.append({"nhan_vien": nv, "tg_hoan_tat": tg_tao})  # Dùng TG Tạo để thống kê CLL
        df_ton = pd.DataFrame(unwrapped_ton)
        if not df_ton.empty:
            dates = pd.to_datetime(df_ton["tg_hoan_tat"], errors="coerce", dayfirst=True, format="mixed").dt.date
            mask = (dates >= month_start_dt) & (dates <= end_dt)
            ton_counts = df_ton[mask]["nhan_vien"].fillna("").astype(str).str.strip().str.upper().value_counts().to_dict()

    # 2. Đọc dữ liệu bảng bao_tri
    bt_records = fetch_all_records("bao_tri")
    bt_counts = {}
    if bt_records:
        unwrapped_bt = []
        for r in bt_records:
            _, nv, tg_hoan, _ = extract_row_fields(r, table="bao_tri")
            unwrapped_bt.append({"nhan_vien": nv, "tg_hoan_tat": tg_hoan})  # Dùng TG Hoàn Tất cho bao_tri
        df_bt = pd.DataFrame(unwrapped_bt)
        if not df_bt.empty:
            dates = pd.to_datetime(df_bt["tg_hoan_tat"], errors="coerce", dayfirst=True, format="mixed").dt.date
            mask = (dates >= month_start_dt) & (dates <= end_dt)
            bt_counts = df_bt[mask]["nhan_vien"].fillna("").astype(str).str.strip().str.upper().value_counts().to_dict()

    team_data = get_team_captains()
    emp_to_captain = team_data.get("emp_to_captain", {})

    all_emps = set(ton_counts.keys()).union(set(bt_counts.keys()))
    cll30_stats = []

    for emp in all_emps:
        if not emp or emp in ("NAN", "NONE"):
            continue
        c_count = ton_counts.get(emp, 0)
        k_count = bt_counts.get(emp, 0)
        total_cls = k_count if k_count > 0 else c_count
        rate = round((c_count / total_cls * 100), 2) if total_cls > 0 else 0.0

        status = "KHONG_DAT" if rate >= 7.0 else "DAT"
        status_text = "Không Đạt (≥7%)" if rate >= 7.0 else "Đạt (<7%)"

        captain = emp_to_captain.get(emp, "Chưa phân công")
        is_captain_member = bool(selected_captain and captain == selected_captain)

        cll30_stats.append({
            "nhan_vien": emp,
            "doi_truong": captain,
            "cll30_count": c_count,
            "total_cls_count": total_cls,
            "rate": rate,
            "status": status,
            "status_text": status_text,
            "is_captain_member": is_captain_member
        })

    cll30_stats.sort(key=lambda x: (x["rate"], x["cll30_count"]), reverse=True)
    return {
        "month_start": str(month_start_dt),
        "month_end": str(end_dt),
        "top_n": cll30_stats[:top_n],
        "total_analyzed_employees": len(cll30_stats)
    }


def import_records(table_name: str, records: list[dict]) -> tuple[bool, str, int]:
    """
    Import/Upsert danh sách bản ghi vào Supabase table (Hỗ trợ bao_tri và ton_bao_tri).
    """
    if not records:
        return False, "Không có dữ liệu để nhập", 0

    if table_name not in ALLOWED_TABLES:
        return False, f"Tên bảng '{table_name}' không nằm trong danh sách hỗ trợ", 0

    headers = get_headers()
    headers["Prefer"] = "resolution=merge-duplicates"

    batch_size = 500
    total_inserted = 0
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table_name}"

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        try:
            resp = requests.post(url, headers=headers, json=batch, timeout=30)
            if resp.status_code in (200, 201, 204):
                total_inserted += len(batch)
            elif resp.status_code == 404:
                log.error(f"Lỗi Supabase 404 Not Found khi import vào {table_name}")
                return False, f"❌ Lỗi 404: Bảng '{table_name}' chưa được tạo trên Supabase của bạn. Vui lòng mở Supabase SQL Editor và chạy câu lệnh tạo bảng.", total_inserted
            elif resp.status_code == 401:
                log.error(f"Lỗi Supabase 401 Unauthorized khi import vào {table_name}")
                return False, "❌ Lỗi 401 Unauthorized: Khóa Supabase API Key không có quyền ghi vào bảng này. Vui lòng kiểm tra lại quyền Supabase.", total_inserted
            else:
                log.error(f"Lỗi import batch vào {table_name}: HTTP {resp.status_code} - {resp.text[:300]}")
                return False, f"Lỗi Supabase HTTP {resp.status_code}: {resp.text[:150]}", total_inserted

        except Exception as e:
            log.error(f"Ngoại lệ khi import vào {table_name}: {e}")
            return False, f"Ngoại lệ kết nối: {str(e)}", total_inserted

    _cache_store.clear()
    _cache_times.clear()
    return True, f"✅ Đã import thành công {total_inserted} bản ghi vào bảng '{table_name}'", total_inserted


def fetch_table_data(table_name: str, page: int = 1, per_page: int = 50, search: str = None) -> dict:
    """Lấy dữ liệu hiển thị phân trang cho bảng bao_tri hoặc ton_bao_tri (Giải nén 100% cột file gốc)."""
    if table_name not in ALLOWED_TABLES:
        return {"records": [], "total": 0, "page": page, "per_page": per_page}

    records = fetch_all_records(table_name, limit=10000)

    # Giải nén data JSONB để hiển thị chuẩn 100% các cột của file gốc
    unwrapped_records = []
    for r in records:
        if isinstance(r.get("data"), dict):
            row = dict(r["data"])
            unwrapped_records.append(row)
        else:
            row = {k: v for k, v in r.items() if k not in ("id", "created_at")}
            unwrapped_records.append(row)

    if search:
        s = search.lower().strip()
        filtered = []
        for r in unwrapped_records:
            if any(s in str(v).lower() for v in r.values()):
                filtered.append(r)
        unwrapped_records = filtered

    total = len(unwrapped_records)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    sliced = unwrapped_records[start_idx:end_idx]

    return {
        "records": sliced,
        "total": total,
        "page": page,
        "per_page": per_page
    }



def clear_table_data(table_name: str) -> tuple[bool, str]:
    """Xóa toàn bộ bản ghi trong bảng bao_tri hoặc ton_bao_tri."""
    if table_name not in ALLOWED_TABLES:
        return False, "Tên bảng không hợp lệ"

    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table_name}?id=gt.0"
    headers = get_headers()
    try:
        resp = requests.delete(url, headers=headers, timeout=15)
        if resp.status_code in (200, 204):
            _cache_store.clear()
            _cache_times.clear()
            return True, f"✅ Đã xóa toàn bộ dữ liệu bảng '{table_name}'"
        else:
            return False, f"Lỗi HTTP {resp.status_code}: {resp.text[:150]}"
    except Exception as e:
        return False, f"Ngoại lệ: {str(e)}"
