"""
sheet_reader.py - Chuyển tiếp toàn bộ truy vấn dữ liệu sang Supabase (thay thế Google Sheet)
"""
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.supabase_db import (
    get_employees as sb_get_employees,
    get_contracts as sb_get_contracts,
    get_team_captains as sb_get_team_captains,
    get_cll30_analytics as sb_get_cll30_analytics
)

log = logging.getLogger('sheet_reader')

def get_employees(force=False, table=None):
    log.info(f"Truy vấn danh sách nhân viên từ Supabase Database (table={table or 'all'})...")
    return sb_get_employees(force=force, table=table)

def get_contracts(selected_employees, start_date=None, end_date=None, lookback_days=2, table='bao_tri'):
    log.info(f"Truy vấn danh sách hợp đồng cho {len(selected_employees)} nhân viên từ bảng [{table}] trên Supabase...")
    return sb_get_contracts(selected_employees, start_date=start_date, end_date=end_date,
                            lookback_days=lookback_days, table=table)

def get_team_captains(force=False):
    log.info("Truy vấn danh sách Đội Trưởng từ Supabase Database...")
    return sb_get_team_captains(force=force)

def get_cll30_analytics(start_date=None, end_date=None, top_n=10, selected_captain=None):
    log.info("Tính toán phân tích chỉ số CLL30N từ Supabase Database...")
    return sb_get_cll30_analytics(start_date=start_date, end_date=end_date, top_n=top_n, selected_captain=selected_captain)