"""
supabase_db.py - Quản lý truy vấn dữ liệu Bảo Trì (PostgreSQL trực tiếp) và Tồn Bảo Trì (Supabase REST API)
Cấu hình 2 nguồn dữ liệu chính:
1. Bảng Bảo Trì (bao_tri):
   - Đơn vị SG01 (Mặc định): lấy trực tiếp từ bảng "bt" trên DEFAULT_DB_URL
   - Đơn vị khác [donvi]: tự động tạo và gán bảng "bt_[donvi]" trên OTHER_UNIT_DB_URL
2. Bảng Tồn Bảo Trì (ton_bao_tri):
   - Quản lý trên Supabase REST API
"""
import logging
import time
import re
from datetime import datetime, timedelta
import requests
import pandas as pd
import sys
import os
import psycopg2
from psycopg2.extras import RealDictCursor

import config
from modules import branch_manager

log = logging.getLogger("supabase_db")
CACHE_TTL = 300

_cache_store = {}
_cache_times = {}
_other_unit_use_pooler = False

ALLOWED_TABLES = ("bao_tri", "bt", "ton_bao_tri", "contracts", "employees", "cll30", "kh_cls")

BT_COLUMNS = [
    "Số_HĐ", "Tên_Khách_Hàng", "Phân_công", "Nhân_viên", "TG_Tạo", "TG_Hoàn_Tất",
    "TG_Online", "Thời_gian_hẹn", "Số_lần_hẹn", "Thời_gian_tích_hẹn_L1", "Mốc_hẹn_L1",
    "Dv_hẹn_L1", "Mốc_hẹn_Xanh_L1", "Dv_hẹn_Xanh_L1", "Thời_gian_nhân_tuyến_cuối",
    "Thời_gian_tích_hẹn_L2", "Mốc_hẹn_L2", "Dv_hẹn_L2", "Mốc_hẹn_Xanh_L2", "Dv_hẹn_Xanh_L2",
    "Thời_gian_phân_công", "Tình_trạng", "Vị_trí_xảy_ra_sự_cố", "Lý_do", "Mô_tả_nguyên_nhân",
    "Tập_điểm", "Loại_checklist", "Đối_tác_quản_lý", "Đối_tác", "Block_quản_lý", "Block",
    "Nguồn", "Ghi_chú", "Tg_checkin_đầu", "Tg_checkin_cuối", "Đơn_vị_hẹn_cuối", "Đúng_hẹn",
    "Tg_DoctorT", "Tg_phân_công_đầu", "Tg_nhận_tuyến_đầu", "Account_tích_L2",
    "Nguyên_nhân_tích_L2", "Tg_tích_cuối", "Account_tạo_CL", "Tình_trang_lên_phiếu",
    "Account_hoàn_tất", "Vùng", "TGXL-24H", "Ghi_chú_CC", "dung_hen",
    "dt_complete", "dt_created", "date_complete", "rt_hours"
]


# ─────────────────────────────────────────────────────────────────────────────
#  POSTGRESQL CONNECTION CHO BẢNG BẢO TRÌ ("bt" & "bt_[donvi]")
# ─────────────────────────────────────────────────────────────────────────────
def _parse_pg_url(db_url: str) -> dict:
    """Phân tách chuỗi kết nối PostgreSQL an toàn (hỗ trợ mật khẩu chứa ký tự '@' như 'Benngo@@2027')."""
    raw = (db_url or "").strip()
    if "://" in raw:
        _, _, rest = raw.partition("://")
    else:
        rest = raw
    authority, _, dbname = rest.partition("/")
    dbname = (dbname.split("?")[0] or "postgres").strip()
    userinfo, _, hostport = authority.rpartition("@")
    user, _, password = userinfo.partition(":")
    if ":" in hostport:
        host, _, port_str = hostport.rpartition(":")
        try:
            port = int(port_str)
        except Exception:
            port = 5432
    else:
        host = hostport
        port = 5432
    return {
        "host": host.strip(),
        "port": port,
        "dbname": dbname,
        "user": user.strip(),
        "password": password,
    }


def _get_bt_pg_connection(branch: str | None = None, connect_timeout: int = 10):
    """
    Mở kết nối PostgreSQL tương ứng với đơn vị/chi nhánh:
    - SG01 / Mặc định -> DEFAULT_DB_URL (aws-0-ap-southeast-1.pooler.supabase.com:6543)
    - Đơn vị khác     -> OTHER_UNIT_DB_URL (tự động fallback sang OTHER_UNIT_POOLER_URL nếu mạng IPv4)
    """
    global _other_unit_use_pooler
    if branch_manager.is_sg01_branch(branch):
        params = _parse_pg_url(getattr(config, "DEFAULT_DB_URL", ""))
        return psycopg2.connect(**params, connect_timeout=connect_timeout)

    direct_url = getattr(config, "OTHER_UNIT_DB_URL", "")
    pooler_url = getattr(config, "OTHER_UNIT_POOLER_URL", "")

    if not _other_unit_use_pooler and direct_url:
        try:
            params = _parse_pg_url(direct_url)
            return psycopg2.connect(**params, connect_timeout=4)
        except Exception as e:
            log.info(f"Kết nối trực tiếp {direct_url.split('@')[-1]} không khả dụng ({e}), chuyển sang pooler IPv4...")
            _other_unit_use_pooler = True

    params = _parse_pg_url(pooler_url or direct_url)
    return psycopg2.connect(**params, connect_timeout=connect_timeout)


def _safe_ident(table_name: str) -> str:
    """Đảm bảo tên bảng hợp lệ để nhúng vào câu lệnh SQL."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", str(table_name or "bt").strip())
    return f'"{clean}"'


def ensure_bt_unit_table(branch: str) -> tuple[bool, str]:
    """
    Tự động khởi tạo bảng bt_[donvi] trên PostgreSQL DB đơn vị mới nếu chưa tồn tại.
    """
    if branch_manager.is_sg01_branch(branch):
        return True, "Đơn vị SG01 sử dụng bảng 'bt' mặc định"

    table_name = branch_manager.get_bt_table_name(branch)
    ident = _safe_ident(table_name)
    cols_ddl = ", ".join(f'"{c}" TEXT' for c in BT_COLUMNS)
    sql = f"CREATE TABLE IF NOT EXISTS {ident} ({cols_ddl});"

    conn = None
    try:
        conn = _get_bt_pg_connection(branch)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
        _cache_store.clear()
        _cache_times.clear()
        log.info(f"✅ Đã khởi tạo/kiểm tra bảng {table_name} cho đơn vị '{branch}'")
        return True, f"Đã khởi tạo bảng '{table_name}' thành công"
    except Exception as e:
        log.error(f"Lỗi tạo bảng {table_name} cho đơn vị '{branch}': {e}")
        return False, str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  SUPABASE REST API (Dùng cho bảng ton_bao_tri)
# ─────────────────────────────────────────────────────────────────────────────
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


def get_branch_variants(branch: str) -> list[str]:
    """Tạo danh sách các biến thể chuỗi chi nhánh để truy vấn Supabase chuẩn xác (NFC, NFD, 01 <-> 1, SG01 <-> Sài Gòn 1)."""
    if not branch or not str(branch).strip():
        return []
    import unicodedata
    raw = str(branch).strip()
    seeds = [raw]
    if branch_manager.is_sg01_branch(raw):
        seeds.extend(["SG01", "Sài Gòn 1", "Sài Gòn 01", "Chi Nhánh Mặc Định"])
    variants = set()
    for s in seeds:
        for b in [s, unicodedata.normalize('NFC', s), unicodedata.normalize('NFD', s)]:
            variants.add(b)
            v_sub = re.sub(r'\b0(\d)\b', r'\1', b)
            variants.add(v_sub)
            v_add = re.sub(r'\b([1-9])\b', r'0\1', b)
            variants.add(v_add)
            variants.add(unicodedata.normalize('NFC', v_sub))
            variants.add(unicodedata.normalize('NFD', v_sub))
            variants.add(unicodedata.normalize('NFC', v_add))
            variants.add(unicodedata.normalize('NFD', v_add))
    return sorted(list(variants))


def build_branch_query_param(branch: str) -> str:
    """Tạo chuỗi filter PostgREST an toàn hỗ trợ mọi biến thể chi nhánh."""
    variants = get_branch_variants(branch)
    if not variants:
        return ""
    or_parts = []
    for v in variants:
        or_parts.append(f"data->>chi_nhanh.eq.{v}")
        or_parts.append(f"data->>Chi nhánh.eq.{v}")
    return "&or=(" + ",".join(or_parts) + ")"


def fetch_all_records(table_name: str, select: str = "*", limit: int = 50000, branch: str = None) -> list[dict]:
    """
    Lấy dữ liệu từ bảng:
    - Nếu là bao_tri / bt / contracts: truy vấn trực tiếp từ PostgreSQL (bảng "bt" hoặc "bt_[donvi]")
    - Nếu là ton_bao_tri: truy vấn qua Supabase REST API
    """
    global _cache_store, _cache_times
    now = time.time()
    b_key = str(branch).strip() if branch else "all"
    cache_key = f"records_{table_name}_{select}_{limit}_{b_key}"
    if cache_key in _cache_store and (now - _cache_times.get(cache_key, 0)) < 120:
        return _cache_store[cache_key]

    if table_name in ("bao_tri", "bt", "contracts"):
        bt_table = branch_manager.get_bt_table_name(branch)
        ident = _safe_ident(bt_table)
        if not branch_manager.is_sg01_branch(branch):
            ensure_bt_unit_table(branch)
        conn = None
        try:
            conn = _get_bt_pg_connection(branch)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT * FROM {ident} LIMIT %s;", (limit,))
                rows = [dict(r) for r in cur.fetchall()]
            _cache_store[cache_key] = rows
            _cache_times[cache_key] = now
            log.info(f"fetch_all_records({bt_table}, branch={branch}): Đã nạp {len(rows)} bản ghi từ PostgreSQL")
            return rows
        except Exception as e:
            log.error(f"Lỗi fetch_all_records PostgreSQL ({bt_table}): {e}")
            return []
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    headers = get_headers()
    base_url = config.SUPABASE_URL.rstrip('/')
    url = f"{base_url}/rest/v1/{table_name}?select={select}"
    if branch and str(branch).strip() and not branch_manager.is_sg01_branch(branch):
        url += build_branch_query_param(str(branch).strip())
    elif branch and str(branch).strip() and branch_manager.is_sg01_branch(branch):
        url += build_branch_query_param(str(branch).strip())

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
            else:
                log.error(f"Lỗi fetch_all_records({table_name}) page {page}: HTTP {r.status_code}")
                break
        except Exception as e:
            log.error(f"Ngoại lệ fetch_all_records({table_name}): {e}")
            break

    if all_records:
        _cache_store[cache_key] = all_records
        _cache_times[cache_key] = now
        log.info(f"fetch_all_records({table_name}, branch={branch}): Đã nạp thành công {len(all_records)} bản ghi từ Supabase")

    return all_records


def _count_bt_records(branch: str | None = None) -> tuple[int, str]:
    """Đếm nhanh tổng số bản ghi trong bảng Bảo Trì (bt hoặc bt_[donvi]) qua PostgreSQL."""
    bt_table = branch_manager.get_bt_table_name(branch)
    ident = _safe_ident(bt_table)
    if not branch_manager.is_sg01_branch(branch):
        ensure_bt_unit_table(branch)

    conn = None
    try:
        conn = _get_bt_pg_connection(branch)
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {ident};")
            row = cur.fetchone()
            return (int(row[0]) if row else 0), bt_table
    except Exception as e:
        log.error(f"Lỗi đếm số lượng bảng {bt_table}: {e}")
        return 0, bt_table
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def fetch_table_summary(branch: str = None) -> dict:
    """Trả về số lượng bản ghi của 2 bảng chính: bao_tri (PostgreSQL bt/bt_[donvi]) và ton_bao_tri (Supabase)."""
    bt_count, bt_table = _count_bt_records(branch)
    bt_info = branch_manager.get_branch_bt_info(branch)

    summary = {
        "bao_tri": bt_count,
        "bt_table": bt_table,
        "bt_db_url": bt_info["db_url"],
        "ton_bao_tri": 0,
    }

    headers = get_headers()
    headers["Prefer"] = "count=exact"
    headers["Range"] = "0-0"
    bp = build_branch_query_param(str(branch).strip()) if branch and str(branch).strip() else ""

    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/ton_bao_tri?select=id{bp}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code in (200, 206):
            content_range = r.headers.get("Content-Range", "")
            if "/" in content_range:
                total = content_range.split("/")[-1]
                summary["ton_bao_tri"] = int(total) if total.isdigit() else len(r.json())
            else:
                summary["ton_bao_tri"] = len(r.json())
    except Exception as e:
        log.error(f"Lỗi đếm số lượng ton_bao_tri: {e}")

    summary["active_branch"] = branch or "SG01"
    return summary


def extract_row_fields(r: dict, table: str = "bao_tri") -> tuple[str, str, str, str]:
    """
    Trích xuất (so_hd, nhan_vien, tg_hoan_tat, tg_tao) chuẩn xác từ bản ghi r (hỗ trợ cả cột có dấu gạch dưới của bảng 'bt').
    """
    if not isinstance(r, dict):
        return "", "", "", ""

    row_data = r.get("data") if isinstance(r.get("data"), dict) else r
    if not isinstance(row_data, dict):
        row_data = {}

    row_vals = list(row_data.values())
    so_hd = ""
    nhan_vien = ""
    tg_hoan_tat = ""
    tg_tao = ""

    is_ton = (table == "ton_bao_tri")

    # 1. SỐ HỢP ĐỒNG (so_hd)
    if is_ton:
        for cand in ["F", "Số HĐ", "SỐ HĐ", "Số_HĐ", "Số HD", "SỐ HD", "Mã HĐ", "MÃ HĐ", "SoHD", "so_hd", "Số Hợp Đồng"]:
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
        top_hd = str(r.get("so_hd") or r.get("Số_HĐ") or "").strip()
        if top_hd and top_hd.upper() not in ("NAN", "NONE", ""):
            top_hd_low = top_hd.lower()
            if not any(x in top_hd_low for x in ("loại", "bảo trì ftth", "trạng thái", "dịch vụ", "nội dung")):
                so_hd = top_hd

    if not so_hd:
        for cand in ["Số_HĐ", "Số HĐ", "SỐ HĐ", "Số HD", "SỐ HD", "Mã HĐ", "MÃ HĐ", "SoHD", "so_hd", "Contract", "Hợp đồng", "hop_dong", "Số Hợp Đồng"]:
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
            if any(x in kl for x in ("hợp đồng", "so_hd", "contract", "số hđ", "số_hđ", "mã hđ")) and not any(x in kl for x in ("loại", "trạng thái", "dịch vụ", "nội dung")):
                val = str(v).strip()
                if val and val.upper() not in ("NAN", "NONE", ""):
                    so_hd = val
                    break

    # 2. THỜI GIAN
    if is_ton:
        for cand in ["H", "TG tạo", "TG_Tạo", "Thời gian tạo", "THỜI GIAN TẠO", "tg_tao", "Ngày tạo", "NGÀY TẠO", "Thời gian", "THỜI GIAN", "Ngày", "NGÀY"]:
            if cand in row_data:
                v = str(row_data[cand]).strip()
                if v and v.upper() not in ("NAN", "NONE", ""):
                    tg_tao = v
                    break
        if not tg_tao and len(row_vals) > 7:
            v = str(row_vals[7]).strip()
            if v and v.upper() not in ("NAN", "NONE", ""):
                tg_tao = v
        tg_hoan_tat = tg_tao
    else:
        top_tg = str(r.get("tg_hoan_tat") or r.get("TG_Hoàn_Tất") or r.get("dt_complete") or r.get("ngay") or "").strip()
        if top_tg and top_tg.upper() not in ("NAN", "NONE", ""):
            tg_hoan_tat = top_tg

        if not tg_hoan_tat:
            for cand in ["TG_Hoàn_Tất", "tg_hoan_tat", "TG hoàn tất", "TG Hoàn Tất", "Thời gian hoàn tất", "THỜI GIAN HOÀN TẤT",
                         "dt_complete", "date_complete", "Ngày hoàn tất", "NGÀY HOÀN TẤT", "Thời gian", "THỜI GIAN", "Ngày", "ngay", "date"]:
                if cand in row_data:
                    v = str(row_data[cand]).strip()
                    if v and v.upper() not in ("NAN", "NONE", ""):
                        tg_hoan_tat = v
                        break

        if not tg_hoan_tat:
            for k, v in row_data.items():
                kl = str(k).lower()
                if any(x in kl for x in ("hoàn tất", "hoàn_tất", "tg_hoan_tat", "dt_complete", "thời gian", "ngày", "date")):
                    val = str(v).strip()
                    if val and val.upper() not in ("NAN", "NONE", ""):
                        tg_hoan_tat = val
                        break

        for cand in ["TG_Tạo", "TG Tạo", "TG tạo", "Thời gian tạo", "tg_tao", "dt_created"]:
            if cand in row_data and row_data[cand]:
                v = str(row_data[cand]).strip()
                if v and v.upper() not in ("NAN", "NONE", ""):
                    tg_tao = v
                    break
        if not tg_tao:
            tg_tao = tg_hoan_tat

    # 3. NHÂN VIÊN (nhan_vien)
    if is_ton:
        for cand in ["S", "Nhân sự", "NHÂN SỰ", "Nhân viên", "Nhân_viên", "NHÂN VIÊN", "nhan_vien", "Account", "inside_account"]:
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
        top_nv = str(r.get("nhan_vien") or r.get("Nhân_viên") or "").strip()
        if top_nv and top_nv.upper() not in ("NAN", "NONE", ""):
            nhan_vien = top_nv

    if not nhan_vien:
        for cand in ["Nhân_viên", "Nhân sự", "nhan_su", "Nhân viên", "nhan_vien", "inside_account", "Account", "NhanVien", "Employee"]:
            if cand in row_data:
                v = str(row_data[cand]).strip()
                if v and v.upper() not in ("NAN", "NONE", ""):
                    nhan_vien = v
                    break

    if not nhan_vien:
        for k, v in row_data.items():
            kl = str(k).lower()
            if any(x in kl for x in ("nhân sự", "nhan_su", "nhân viên", "nhân_viên", "nhan_vien", "account")):
                val = str(v).strip()
                if val and val.upper() not in ("NAN", "NONE", ""):
                    nhan_vien = val
                    break

    return so_hd, nhan_vien, tg_hoan_tat, tg_tao


def extract_branch(r: dict) -> str:
    """Trích xuất tên chi nhánh từ bản ghi r hoặc r['data']."""
    if not isinstance(r, dict):
        return ""
    row_data = r.get("data") if isinstance(r.get("data"), dict) else r
    if not isinstance(row_data, dict):
        row_data = {}
    for cand in ["chi_nhanh", "Chi nhánh", "Chi Nhánh", "CN", "chi_nhanh_name", "branch", "Đối_tác"]:
        if cand in row_data and row_data[cand]:
            v = str(row_data[cand]).strip()
            if v and v.upper() not in ("NAN", "NONE", ""):
                return v
    if "chi_nhanh" in r and r["chi_nhanh"]:
        v = str(r["chi_nhanh"]).strip()
        if v and v.upper() not in ("NAN", "NONE", ""):
            return v
    return ""


def _fetch_bt_employees_pg(branch: str | None = None) -> list[str]:
    """Lấy nhanh danh sách nhân viên duy nhất từ bảng PostgreSQL (bt hoặc bt_[donvi])."""
    bt_table = branch_manager.get_bt_table_name(branch)
    ident = _safe_ident(bt_table)
    if not branch_manager.is_sg01_branch(branch):
        ensure_bt_unit_table(branch)

    conn = None
    try:
        conn = _get_bt_pg_connection(branch)
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT DISTINCT TRIM("Nhân_viên") FROM {ident} '
                f'WHERE "Nhân_viên" IS NOT NULL AND TRIM("Nhân_viên") <> \'\' AND LOWER(TRIM("Nhân_viên")) <> \'nan\';'
            )
            return sorted([r[0] for r in cur.fetchall() if r and r[0]])
    except Exception as e:
        log.error(f"Lỗi _fetch_bt_employees_pg({bt_table}): {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_employees(force: bool = False, table: str = None, branch: str = None) -> list[str]:
    """
    Lấy danh sách nhân viên (có hỗ trợ lọc theo branch / đơn vị):
    - table='bao_tri'   → truy vấn SQL DISTINCT trực tiếp trên bảng "bt" / "bt_[donvi]"
    - table='ton_bao_tri' → truy vấn từ bảng ton_bao_tri trên Supabase
    - table=None        → gộp cả 2 bảng
    """
    global _cache_store, _cache_times
    now = time.time()
    b_key = branch.strip() if branch else "all"
    cache_key = f"employees_{table or 'all'}_{b_key}"
    if not force and cache_key in _cache_store and (now - _cache_times.get(cache_key, 0)) < CACHE_TTL:
        return _cache_store[cache_key]

    employees = set()

    if table in (None, "bao_tri", "bt"):
        for nv in _fetch_bt_employees_pg(branch):
            employees.add(nv)

    if table in (None, "ton_bao_tri"):
        recs = fetch_all_records("ton_bao_tri", branch=branch)
        for r in recs:
            _, nv, _, _ = extract_row_fields(r, table="ton_bao_tri")
            if nv and nv.upper() not in ("NAN", "NONE", ""):
                employees.add(nv)

    result = sorted(list(employees))
    _cache_store[cache_key] = result
    _cache_times[cache_key] = now
    log.info(f"get_employees(table={table}, branch={branch}): {len(result)} nhân viên")
    return result


def _get_bt_contracts_pg(selected_employees: list, start_date=None, end_date=None, branch: str = None) -> list[dict]:
    """Truy vấn hợp đồng Bảo Trì trực tiếp bằng SQL từ bảng 'bt' hoặc 'bt_[donvi]'."""
    bt_table = branch_manager.get_bt_table_name(branch)
    ident = _safe_ident(bt_table)
    if not branch_manager.is_sg01_branch(branch):
        ensure_bt_unit_table(branch)

    selected_upper = [str(e).strip().upper() for e in selected_employees if str(e).strip()]
    if not selected_upper:
        return []

    conn = None
    rows = []
    try:
        conn = _get_bt_pg_connection(branch)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sql = (
                f'SELECT "Số_HĐ", "Nhân_viên", "TG_Hoàn_Tất", "TG_Tạo", "date_complete", "dt_complete" '
                f'FROM {ident} '
                f'WHERE UPPER(TRIM("Nhân_viên")) = ANY(%s);'
            )
            cur.execute(sql, (selected_upper,))
            rows = cur.fetchall()
    except Exception as e:
        log.error(f"Lỗi truy vấn hợp đồng từ bảng {bt_table}: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    if not rows:
        return []

    unwrapped_list = []
    for r in rows:
        so_hd = str(r.get("Số_HĐ") or "").strip()
        nhan_vien = str(r.get("Nhân_viên") or "").strip()
        tg_hoan_tat = str(r.get("TG_Hoàn_Tất") or r.get("dt_complete") or "").strip()
        tg_tao = str(r.get("TG_Tạo") or "").strip()
        date_for_filter = str(r.get("TG_Hoàn_Tất") or r.get("dt_complete") or r.get("date_complete") or "").strip()
        if not so_hd or so_hd.upper() in ("NAN", "NONE"):
            continue
        unwrapped_list.append({
            "so_hd": so_hd,
            "nhan_vien": nhan_vien,
            "tg_hoan_tat": tg_hoan_tat,
            "tg_tao": tg_tao,
            "date_for_filter": date_for_filter,
        })

    df = pd.DataFrame(unwrapped_list)
    if df.empty:
        return []

    parsed_dates = pd.to_datetime(df["date_for_filter"], errors="coerce", format="mixed", dayfirst=True)
    df["_datetime"] = parsed_dates
    df["_date"] = parsed_dates.dt.date

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
        filtered = df[mask_date].copy()
    else:
        filtered = df.copy()

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
            "data_source": "bao_tri",
        })

    log.info(f"PostgreSQL ({bt_table}): Đã lọc {len(results)} hợp đồng Bảo Trì cho {len(selected_upper)} nhân viên ({start_date} -> {end_date})")
    return results


def get_contracts(selected_employees: list, start_date=None, end_date=None,
                  lookback_days: int = 2, table: str = "bao_tri", branch: str = None) -> list[dict]:
    """
    Truy vấn danh sách hợp đồng từ bảng chỉ định:
    - bao_tri -> Truy vấn SQL trực tiếp từ bảng PostgreSQL ("bt" cho SG01 hoặc "bt_[donvi]" cho đơn vị khác)
    - ton_bao_tri -> Truy vấn từ Supabase REST API
    """
    if not selected_employees:
        return []

    if table not in ("bao_tri", "ton_bao_tri"):
        table = "bao_tri"

    if table == "bao_tri":
        return _get_bt_contracts_pg(selected_employees, start_date=start_date, end_date=end_date, branch=branch)

    records = fetch_all_records(table, branch=branch)
    if not records:
        log.warning(f"Bảng {table} (branch={branch}) trên Supabase rỗng.")
        return []

    unwrapped_list = []
    for r in records:
        so_hd, nhan_vien, tg_hoan_tat, tg_tao = extract_row_fields(r, table=table)
        row_data = r.get("data") if isinstance(r.get("data"), dict) else r
        rec_branch = extract_branch(r)
        date_for_filter = tg_tao if table == "ton_bao_tri" else tg_hoan_tat

        unwrapped_list.append({
            "so_hd": str(so_hd or "").strip(),
            "nhan_vien": str(nhan_vien or "").strip(),
            "tg_hoan_tat": str(tg_hoan_tat or "").strip(),
            "tg_tao": str(tg_tao or "").strip(),
            "date_for_filter": str(date_for_filter or "").strip(),
            "chi_nhanh": rec_branch,
            "raw_row": row_data
        })

    df = pd.DataFrame(unwrapped_list)
    if df.empty or "nhan_vien" not in df.columns:
        return []

    date_col = "date_for_filter" if "date_for_filter" in df.columns else "tg_hoan_tat"
    parsed_dates = pd.to_datetime(df[date_col], errors="coerce", format="mixed", dayfirst=True)
    df["_datetime"] = parsed_dates
    df["_date"] = parsed_dates.dt.date

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
        mask_date = pd.Series(True, index=df.index)

    nv_col = df["nhan_vien"].fillna("").astype(str).str.strip().str.upper()
    selected_stripped = [str(e).strip().upper() for e in selected_employees if str(e).strip()]
    mask_nv = nv_col.isin(selected_stripped)

    filtered = df[mask_date & mask_nv].copy()
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

    log.info(f"Supabase ({table}): Lọc theo [TG Tạo] – Đã lọc chính xác {len(results)} hợp đồng cho nhân viên {selected_employees} từ ngày {start_date} đến {end_date}")
    return results


def get_team_captains(force: bool = False, branch: str = None) -> dict:
    """Lấy danh sách Đội Trưởng từ cấu hình local team_captains.json."""
    global _cache_store, _cache_times
    now = time.time()
    b_key = branch.strip() if branch else "all"
    cache_key = f"captains_{b_key}"
    if not force and cache_key in _cache_store and (now - _cache_times.get(cache_key, 0)) < CACHE_TTL:
        return _cache_store[cache_key]

    try:
        from modules import captain_manager
        local_data = captain_manager.get_captains(branch=branch)
        if local_data.get("sorted_captains"):
            _cache_store[cache_key] = local_data
            _cache_times[cache_key] = now
            return local_data
    except Exception as e_local:
        log.warning(f"Lỗi đọc captain_manager: {e_local}")

    res = {
        "captains_map": {},
        "sorted_captains": [],
        "emp_to_captain": {},
        "branch": branch or "SG01"
    }
    _cache_store[cache_key] = res
    _cache_times[cache_key] = now
    return res


def get_cll30_analytics(start_date=None, end_date=None, top_n=10, selected_captain=None) -> dict:
    """
    Tính Tỷ lệ Tồn Bảo Trì dựa trên bảng ton_bao_tri (Supabase) và bảng bt (PostgreSQL).
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
            unwrapped_ton.append({"nhan_vien": nv, "tg_hoan_tat": tg_tao})
        df_ton = pd.DataFrame(unwrapped_ton)
        if not df_ton.empty:
            dates = pd.to_datetime(df_ton["tg_hoan_tat"], errors="coerce", dayfirst=True, format="mixed").dt.date
            mask = (dates >= month_start_dt) & (dates <= end_dt)
            ton_counts = df_ton[mask]["nhan_vien"].fillna("").astype(str).str.strip().str.upper().value_counts().to_dict()

    # 2. Đọc dữ liệu bảng bt từ PostgreSQL
    bt_counts = {}
    conn = None
    try:
        conn = _get_bt_pg_connection("SG01")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT UPPER(TRIM("Nhân_viên")) AS nv, "TG_Hoàn_Tất", "date_complete" FROM "bt" WHERE "Nhân_viên" IS NOT NULL;')
            bt_rows = cur.fetchall()
        if bt_rows:
            df_bt = pd.DataFrame(bt_rows)
            dates = pd.to_datetime(df_bt["date_complete"].fillna(df_bt["TG_Hoàn_Tất"]), errors="coerce", dayfirst=True, format="mixed").dt.date
            mask = (dates >= month_start_dt) & (dates <= end_dt)
            bt_counts = df_bt[mask]["nv"].fillna("").astype(str).str.strip().str.upper().value_counts().to_dict()
    except Exception as e:
        log.error(f"Lỗi thống kê CLL30 từ bảng bt: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

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


def normalize_date_key(dt_val) -> str:
    if not dt_val:
        return ""
    val_str = str(dt_val).strip()
    try:
        dt = pd.to_datetime(val_str, errors='coerce', dayfirst=True)
        if pd.notna(dt):
            return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        pass
    return val_str


def extract_bao_tri_key(r: dict, col_names: list = None) -> tuple[str, str, str, str, str]:
    row_data = r.get("data") if isinstance(r.get("data"), dict) else r
    if not isinstance(row_data, dict):
        row_data = {}

    row_vals = [row_data.get(c, "") for c in col_names] if col_names else list(row_data.values())

    so_hd = str(r.get("so_hd") or r.get("Số_HĐ") or "").strip()
    if not so_hd:
        for cand in ["Số_HĐ", "Số HĐ", "Số HD", "SỐ HĐ", "SỐ HD", "Mã HĐ", "MÃ HĐ", "Số Hợp Đồng", "SoHD", "so_hd", "Contract"]:
            if cand in row_data and str(row_data[cand]).strip():
                so_hd = str(row_data[cand]).strip()
                break
    if not so_hd and len(row_vals) > 0:
        v0 = str(row_vals[0]).strip()
        if v0 and v0.upper() not in ("NAN", "NONE", ""):
            so_hd = v0

    nhan_vien = str(r.get("nhan_vien") or r.get("Nhân_viên") or "").strip()
    if not nhan_vien:
        for cand in ["Nhân_viên", "Nhân viên", "Nhân sự", "nhan_vien", "nhan_su", "NHÂN VIÊN", "Nhân Viên", "KTV"]:
            if cand in row_data and str(row_data[cand]).strip():
                nhan_vien = str(row_data[cand]).strip()
                break
    if not nhan_vien and len(row_vals) > 3:
        v3 = str(row_vals[3]).strip()
        if v3 and v3.upper() not in ("NAN", "NONE", ""):
            nhan_vien = v3

    tg_tao = ""
    for cand in ["TG_Tạo", "TG Tạo", "TG Tao", "Thời gian tạo", "Thời Gian Tạo", "tg_tao", "TG TẠO", "Ngay Tao"]:
        if cand in row_data and str(row_data[cand]).strip():
            tg_tao = str(row_data[cand]).strip()
            break
    if not tg_tao and len(row_vals) > 4:
        v4 = str(row_vals[4]).strip()
        if v4 and v4.upper() not in ("NAN", "NONE", ""):
            tg_tao = v4

    tg_hoan_tat = str(r.get("tg_hoan_tat") or r.get("TG_Hoàn_Tất") or "").strip()
    if not tg_hoan_tat:
        for cand in ["TG_Hoàn_Tất", "TG Hoàn Tất", "TG Hoan Tat", "Thời gian hoàn tất", "Thời Gian Hoàn Tất", "tg_hoan_tat", "TG HOÀN TẤT"]:
            if cand in row_data and str(row_data[cand]).strip():
                tg_hoan_tat = str(row_data[cand]).strip()
                break
    if not tg_hoan_tat and len(row_vals) > 5:
        v5 = str(row_vals[5]).strip()
        if v5 and v5.upper() not in ("NAN", "NONE", ""):
            tg_hoan_tat = v5

    account_tao_cl = ""
    for cand in ["Account_tạo_CL", "Account tạo CL", "Account Tạo Cl", "Account Tạo CL", "Account tao cl", "Account tạo cl", "account_tao_cl", "AccountTaoCL"]:
        if cand in row_data and str(row_data[cand]).strip():
            account_tao_cl = str(row_data[cand]).strip()
            break
    if not account_tao_cl and len(row_vals) > 43:
        v43 = str(row_vals[43]).strip()
        if v43 and v43.upper() not in ("NAN", "NONE", ""):
            account_tao_cl = v43

    return (
        so_hd.strip().upper(),
        normalize_date_key(tg_tao),
        normalize_date_key(tg_hoan_tat),
        nhan_vien.strip().upper(),
        account_tao_cl.strip().upper(),
    )


def fetch_existing_bao_tri_keys(so_hd_list: list, branch: str = None) -> dict:
    return {}


def deduplicate_existing_bao_tri(branch: str = None) -> tuple[bool, str, int]:
    bt_table = branch_manager.get_bt_table_name(branch)
    return True, f"✅ Bảng '{bt_table}' được đồng bộ trực tiếp từ PostgreSQL, dữ liệu đã chuẩn hóa!", 0


def import_records(table_name: str, records: list[dict]) -> tuple[bool, str, int]:
    """Import/Upsert danh sách bản ghi vào bảng ton_bao_tri trên Supabase."""
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
        raw_batch = records[i:i + batch_size]
        if not raw_batch:
            continue

        keys_set = set()
        for r in raw_batch:
            keys_set.update(r.keys())

        batch = []
        for r in raw_batch:
            item = {}
            for k in sorted(keys_set):
                item[k] = r.get(k, None)
            batch.append(item)

        try:
            resp = requests.post(url, headers=headers, json=batch, timeout=40)
            if resp.status_code in (200, 201, 204):
                total_inserted += len(batch)
            elif resp.status_code == 404:
                return False, f"❌ Lỗi 404: Bảng '{table_name}' chưa được tạo trên Supabase của bạn.", total_inserted
            elif resp.status_code == 401:
                return False, "❌ Lỗi 401 Unauthorized: Khóa Supabase API Key không có quyền ghi.", total_inserted
            else:
                return False, f"Lỗi Supabase HTTP {resp.status_code}: {resp.text[:150]}", total_inserted
        except Exception as e:
            return False, f"Ngoại lệ kết nối: {str(e)}", total_inserted

    _cache_store.clear()
    _cache_times.clear()
    return True, f"✅ Đã lưu thành công {total_inserted} bản ghi vào bảng '{table_name}'", total_inserted


def fetch_table_data(table_name: str, page: int = 1, per_page: int = 50, search: str = None, branch: str = None) -> dict:
    """
    Lấy dữ liệu hiển thị phân trang:
    - bao_tri / bt / contracts -> Truy vấn SQL phân trang trực tiếp từ bảng "bt" hoặc "bt_[donvi]" trên PostgreSQL
    - ton_bao_tri -> Truy vấn từ Supabase REST API
    """
    if table_name not in ALLOWED_TABLES:
        return {"records": [], "total": 0, "page": page, "per_page": per_page}

    if table_name in ("bao_tri", "bt", "contracts"):
        bt_table = branch_manager.get_bt_table_name(branch)
        ident = _safe_ident(bt_table)
        if not branch_manager.is_sg01_branch(branch):
            ensure_bt_unit_table(branch)

        offset = max(0, (page - 1) * per_page)
        conn = None
        try:
            conn = _get_bt_pg_connection(branch)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if search and search.strip():
                    s_pat = f"%{search.strip()}%"
                    where_sql = (
                        'WHERE COALESCE("Số_HĐ",\'\') ILIKE %s '
                        'OR COALESCE("Nhân_viên",\'\') ILIKE %s '
                        'OR COALESCE("Tên_Khách_Hàng",\'\') ILIKE %s '
                        'OR COALESCE("Tập_điểm",\'\') ILIKE %s '
                        'OR COALESCE("Ghi_chú",\'\') ILIKE %s '
                        'OR COALESCE("TG_Hoàn_Tất",\'\') ILIKE %s'
                    )
                    params_cnt = (s_pat, s_pat, s_pat, s_pat, s_pat, s_pat)
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM {ident} {where_sql};", params_cnt)
                    total = int((cur.fetchone() or {}).get("cnt") or 0)

                    cur.execute(
                        f'SELECT * FROM {ident} {where_sql} ORDER BY "dt_complete" DESC NULLS LAST LIMIT %s OFFSET %s;',
                        params_cnt + (per_page, offset)
                    )
                    rows = [dict(r) for r in cur.fetchall()]
                else:
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM {ident};")
                    total = int((cur.fetchone() or {}).get("cnt") or 0)

                    cur.execute(
                        f'SELECT * FROM {ident} ORDER BY "dt_complete" DESC NULLS LAST LIMIT %s OFFSET %s;',
                        (per_page, offset)
                    )
                    rows = [dict(r) for r in cur.fetchall()]

            return {
                "records": rows,
                "total": total,
                "page": page,
                "per_page": per_page,
                "bt_table": bt_table,
            }
        except Exception as e:
            log.error(f"Lỗi fetch_table_data PostgreSQL ({bt_table}): {e}")
            return {"records": [], "total": 0, "page": page, "per_page": per_page, "bt_table": bt_table}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    records = fetch_all_records(table_name, limit=10000, branch=branch)
    unwrapped_records = []
    for r in records:
        if isinstance(r.get("data"), dict):
            row = dict(r["data"])
            if "chi_nhanh" not in row and extract_branch(r):
                row["chi_nhanh"] = extract_branch(r)
            unwrapped_records.append(row)
        else:
            row = {k: v for k, v in r.items() if k not in ("id", "created_at")}
            if "chi_nhanh" not in row and extract_branch(r):
                row["chi_nhanh"] = extract_branch(r)
            unwrapped_records.append(row)

    if search:
        s = search.lower().strip()
        unwrapped_records = [
            r for r in unwrapped_records
            if any(s in str(v).lower() for v in r.values())
        ]

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


def clear_table_data(table_name: str, branch: str = None) -> tuple[bool, str]:
    """Xóa bản ghi trong bảng ton_bao_tri (bảng Bảo Trì lấy trực tiếp từ PostgreSQL không xóa từ giao diện)."""
    if table_name not in ALLOWED_TABLES:
        return False, "Tên bảng không hợp lệ"

    if table_name in ("bao_tri", "bt", "contracts"):
        bt_table = branch_manager.get_bt_table_name(branch)
        return False, f"Bảng Bảo Trì ({bt_table}) được đọc trực tiếp từ PostgreSQL, không hỗ trợ xóa dữ liệu từ giao diện."

    if branch and str(branch).strip():
        bp = build_branch_query_param(str(branch).strip())
        url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table_name}?id=gt.0{bp}"
    else:
        url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table_name}?id=gt.0"

    headers = get_headers()
    headers["Prefer"] = "return=minimal"
    try:
        resp = requests.delete(url, headers=headers, timeout=60)
        if resp.status_code in (200, 204):
            _cache_store.clear()
            _cache_times.clear()
            target_str = f"của chi nhánh '{branch}'" if branch else "toàn bộ"
            return True, f"✅ Đã xóa dữ liệu {target_str} trong bảng '{table_name}'"
        else:
            return False, f"Lỗi HTTP {resp.status_code}: {resp.text[:150]}"
    except Exception as e:
        return False, f"Ngoại lệ: {str(e)}"
