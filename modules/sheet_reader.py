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

def get_contracts(selected_employees, lookback_days=2):
    if not selected_employees:
        return []

    df = get_sheet_df()
    raw_dates = df.iloc[:, COL_TG_HOAN_TAT]
    parsed = pd.to_datetime(raw_dates, errors='coerce', dayfirst=True)
    df['_date'] = parsed.dt.date

    today = datetime.now().date()
    # Bao gồm cả ngày Hôm Nay (Day 0), Hôm Qua (Day 1), và Hôm Kia (Day 2)
    target_dates = {today - timedelta(days=i) for i in range(0, lookback_days + 1)}
    mask_date = df['_date'].isin(target_dates)
    nv_col = df.iloc[:, COL_NHAN_VIEN].fillna('').astype(str).str.strip().str.upper()
    selected_stripped = [str(e).strip().upper() for e in selected_employees if str(e).strip()]
    mask_nv = nv_col.isin(selected_stripped)
    filtered = df[mask_date & mask_nv].copy()

    # Fallback 1: Thử mở rộng 14 ngày nếu chưa có dữ liệu
    if filtered.empty and lookback_days < 14:
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