import io, logging, time
from datetime import datetime, timedelta
import requests, pandas as pd
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import SHEET_ID, SHEET_GID

log = logging.getLogger('sheet_reader')
COL_SO_HD = 0
COL_NHAN_VIEN = 3
COL_TG_HOAN_TAT = 5
_df_cache = None
_cache_time = 0
CACHE_TTL = 300

def _get_csv_url():
    return f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}'

def get_sheet_df(force=False):
    global _df_cache, _cache_time
    now = time.time()
    if not force and _df_cache is not None and (now - _cache_time) < CACHE_TTL:
        return _df_cache
    url = _get_csv_url()
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = requests.get(url, timeout=30, headers=headers, allow_redirects=True)
        resp.encoding = 'utf-8'
        if resp.status_code == 200:
            df = pd.read_csv(io.StringIO(resp.text), header=0, dtype=str)
            log.info(f'Sheet: {len(df)} dong, {len(df.columns)} cot')
            _df_cache = df
            _cache_time = now
            return df
        else:
            raise RuntimeError(f'Sheet HTTP {resp.status_code}')
    except requests.exceptions.Timeout:
        raise RuntimeError('Timeout 30s khi doc Google Sheet')
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f'Loi mang: {e}')

def get_employees():
    df = get_sheet_df()
    col = df.iloc[:, COL_NHAN_VIEN]
    employees = col.dropna().str.strip().unique().tolist()
    return sorted([e for e in employees if e and e != 'nan'])

def get_contracts(selected_employees, start_date=None, end_date=None, lookback_days=2):
    if not selected_employees:
        return []

    df = get_sheet_df()
    raw_dates = df.iloc[:, COL_TG_HOAN_TAT]
    parsed = pd.to_datetime(raw_dates, errors='coerce', dayfirst=True)
    df['_date'] = parsed.dt.date

    if start_date or end_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date() if isinstance(start_date, str) else start_date
        except Exception:
            start_dt = None
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date() if isinstance(end_date, str) else end_date
        except Exception:
            end_dt = None

        if start_dt and not end_dt:
            end_dt = datetime.now().date()
        elif end_dt and not start_dt:
            start_dt = end_dt - timedelta(days=lookback_days)

        if start_dt and end_dt:
            if start_dt > end_dt:
                start_dt, end_dt = end_dt, start_dt
            mask_date = (df['_date'] >= start_dt) & (df['_date'] <= end_dt)
        else:
            today = datetime.now().date()
            target_dates = {today - timedelta(days=i) for i in range(0, lookback_days + 1)}
            mask_date = df['_date'].isin(target_dates)
    else:
        today = datetime.now().date()
        target_dates = {today - timedelta(days=i) for i in range(0, lookback_days + 1)}
        mask_date = df['_date'].isin(target_dates)

    nv_col = df.iloc[:, COL_NHAN_VIEN].fillna('').astype(str).str.strip().str.upper()
    selected_stripped = [str(e).strip().upper() for e in selected_employees if str(e).strip()]
    mask_nv = nv_col.isin(selected_stripped)
    filtered = df[mask_date & mask_nv].copy()

    # Fallback 1: Thử mở rộng 14 ngày nếu chọn mặc định và chưa có dữ liệu
    if filtered.empty and not (start_date or end_date) and lookback_days < 14:
        log.info(f"Không có HĐ trong {lookback_days} ngày, mở rộng tìm trong 14 ngày...")
        return get_contracts(selected_employees, lookback_days=14)

    # Fallback 2: Lấy các hợp đồng mới nhất của nhân viên đã chọn
    if filtered.empty:
        log.info("Mở rộng lấy hợp đồng gần nhất của nhân viên đã chọn trong Sheet...")
        filtered = df[mask_nv].copy()
        if not filtered.empty:
            filtered = filtered.sort_values(by='_date', ascending=False).head(50)

    if filtered.empty:
        return []

    results = []
    seen = set()
    for _, row in filtered.iterrows():
        so_hd = str(row.iloc[COL_SO_HD]).strip()
        if so_hd in ('', 'nan', 'None') or so_hd in seen:
            continue
        seen.add(so_hd)
        results.append({
            'so_hd':       so_hd,
            'nhan_vien':   str(row.iloc[COL_NHAN_VIEN]).strip(),
            'tg_hoan_tat': str(row.iloc[COL_TG_HOAN_TAT]).strip(),
            'ngay':        str(row['_date']) if pd.notna(row['_date']) else '',
        })
    log.info(f'Tìm được {len(results)} hợp đồng cho {len(selected_employees)} nhân viên')
    return results

# ============================================================
# BỔ SUNG CÁC TÍNH NĂNG MỚI (ĐỘI TRƯỞNG & CLL30N)
# ============================================================

SHEET_NHAN_SU_GID = '32204814'
SHEET_CLL30N_GID = '168764867'
SHEET_KH_CLS_ID = '1JtMBIXmgQ37ne9a_QYb6ZuN6mWwgJHIC-kSjKEb-56w'
SHEET_KH_CLS_GID = '0'

_team_captains_cache = None
_team_captains_cache_time = 0

def fetch_raw_sheet(sheet_id, gid):
    url = f'https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}'
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = requests.get(url, timeout=30, headers=headers, allow_redirects=True)
    resp.encoding = 'utf-8'
    if resp.status_code == 200:
        return pd.read_csv(io.StringIO(resp.text), header=0, dtype=str)
    raise RuntimeError(f'HTTP {resp.status_code} when reading sheet {sheet_id} gid {gid}')

def get_team_captains(force=False):
    """
    Lấy danh sách Đội Trưởng và danh sách Inside Account của từng Đội Trưởng
    từ Sheet Nhân Sự (GID 32204814).
    Col G (index 6): Inside Account (PNC01.xxx)
    Col Y (index 24): Họ tên Đội trưởng
    """
    global _team_captains_cache, _team_captains_cache_time
    now = time.time()
    if not force and _team_captains_cache is not None and (now - _team_captains_cache_time) < CACHE_TTL:
        return _team_captains_cache

    try:
        df = fetch_raw_sheet(SHEET_ID, SHEET_NHAN_SU_GID)
        captain_col_idx = 24  # Col Y
        account_col_idx = 6   # Col G

        if len(df.columns) > captain_col_idx:
            captains_map = {}
            emp_to_captain = {}
            for _, row in df.iterrows():
                account = str(row.iloc[account_col_idx]).strip().upper() if pd.notna(row.iloc[account_col_idx]) else ""
                captain = str(row.iloc[captain_col_idx]).strip() if pd.notna(row.iloc[captain_col_idx]) else ""
                if account and account not in ('NAN', 'NONE', ''):
                    if captain and captain not in ('nan', 'None', ''):
                        if captain not in captains_map:
                            captains_map[captain] = []
                        captains_map[captain].append(account)
                        emp_to_captain[account] = captain
            
            # Sắp xếp danh sách
            sorted_captains = sorted(captains_map.keys())
            res = {
                'captains_map': captains_map,
                'sorted_captains': sorted_captains,
                'emp_to_captain': emp_to_captain
            }
            _team_captains_cache = res
            _team_captains_cache_time = now
            return res
    except Exception as e:
        log.error(f"Lỗi khi đọc Sheet Nhân Sự (Đội trưởng): {e}")
    
    return {'captains_map': {}, 'sorted_captains': [], 'emp_to_captain': {}}


_cll30_cache = None
_cll30_cache_time = 0

def get_cll30_analytics(start_date=None, end_date=None, top_n=10, selected_captain=None):
    """
    Tính Tỷ lệ CLL30N theo Nhân viên:
    Tỷ lệ % = (Tổng HĐ trong SHEET CLL30N) / (Tổng HĐ trong SHEET KH Có Cls) * 100%
    Đánh giá: < 7% là Đạt, >= 7% là Không Đạt.
    Lọc xét trong Tháng tính đến ngày kết thúc phân tích.
    """
    global _cll30_cache, _cll30_cache_time
    now = time.time()
    
    # Xác định khoảng thời gian xét tháng
    try:
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
        else:
            end_dt = datetime.now().date()
    except Exception:
        end_dt = datetime.now().date()

    month_start_dt = end_dt.replace(day=1)

    try:
        # 1. Đọc Sheet CLL30N (GID 168764867)
        df_cll30 = fetch_raw_sheet(SHEET_ID, SHEET_CLL30N_GID)
        # Col D (index 3): Nhân viên, Col G (index 6): Tg hoàn tất
        cll30_nv_col = df_cll30.iloc[:, 3].fillna('').astype(str).str.strip().str.upper()
        cll30_dates = pd.to_datetime(df_cll30.iloc[:, 6], errors='coerce', dayfirst=True).dt.date
        mask_cll30_month = (cll30_dates >= month_start_dt) & (cll30_dates <= end_dt)
        filtered_cll30 = df_cll30[mask_cll30_month]
        cll30_counts = filtered_cll30.iloc[:, 3].fillna('').astype(str).str.strip().str.upper().value_counts().to_dict()

        # 2. Đọc Sheet KH Có Cls (GID 0)
        df_kh_cls = fetch_raw_sheet(SHEET_KH_CLS_ID, SHEET_KH_CLS_GID)
        # Col D (index 3): Nhân viên, Col G (index 6): Tg hoàn tất
        kh_nv_col = df_kh_cls.iloc[:, 3].fillna('').astype(str).str.strip().str.upper()
        kh_dates = pd.to_datetime(df_kh_cls.iloc[:, 6], errors='coerce', dayfirst=True).dt.date
        mask_kh_month = (kh_dates >= month_start_dt) & (kh_dates <= end_dt)
        filtered_kh = df_kh_cls[mask_kh_month]
        kh_counts = filtered_kh.iloc[:, 3].fillna('').astype(str).str.strip().str.upper().value_counts().to_dict()

        # 3. Lấy mapping Đội Trưởng
        team_data = get_team_captains()
        emp_to_captain = team_data.get('emp_to_captain', {})

        # 4. Gom danh sách nhân viên
        all_emps = set(cll30_counts.keys()).union(set(kh_counts.keys()))
        cll30_stats = []

        for emp in all_emps:
            if not emp or emp in ('NAN', 'NONE'):
                continue
            c_count = cll30_counts.get(emp, 0)
            k_count = kh_counts.get(emp, 0)
            
            # Nếu tổng KH Cls = 0 nhưng có CLL30, mặc định k_count = max(c_count, 1)
            total_cls = k_count if k_count > 0 else c_count
            rate = round((c_count / total_cls * 100), 2) if total_cls > 0 else 0.0
            
            status = "KHONG_DAT" if rate >= 7.0 else "DAT"
            status_text = "Không Đạt (≥7%)" if rate >= 7.0 else "Đạt (<7%)"

            captain = emp_to_captain.get(emp, "Chưa phân công")
            is_captain_member = False
            if selected_captain and captain == selected_captain:
                is_captain_member = True

            cll30_stats.append({
                'nhan_vien': emp,
                'doi_truong': captain,
                'cll30_count': c_count,
                'total_cls_count': total_cls,
                'rate': rate,
                'status': status,
                'status_text': status_text,
                'is_captain_member': is_captain_member
            })

        # Sắp xếp giảm dần theo Tỷ lệ CLL30N % và số lượng CLL30N
        cll30_stats.sort(key=lambda x: (x['rate'], x['cll30_count']), reverse=True)

        top_employees = cll30_stats[:top_n]
        return {
            'month_start': str(month_start_dt),
            'month_end': str(end_dt),
            'top_n': top_employees,
            'total_analyzed_employees': len(cll30_stats)
        }

    except Exception as e:
        log.error(f"Lỗi khi tính toán phân tích CLL30N: {e}")
        return {
            'month_start': str(month_start_dt),
            'month_end': str(end_dt),
            'top_n': [],
            'total_analyzed_employees': 0,
            'error': str(e)
        }