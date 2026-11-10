# ============================================================
#   CAU HINH MYBAE AUTO DASHBOARD
# ============================================================
import os

# === Tai khoan FPT ===
EMAIL = 'phuongnam.phongnh5@fpt.net'
EMAIL_PASSWORD = 'Benngo@@20262027'

# === FPT Inside Account ===
INSIDE_ACCOUNT  = 'phuongnam.phongnh5'
INSIDE_PASSWORD = 'Benngo@@2026'
INSIDE_FPT_LOGIN_URL = 'http://inside.fpt.net'
INSIDE_FPT_URL       = 'http://inside.fpt.net'

# === Google Authenticator TOTP (sẽ cập nhật sau khi scan QR) ===
# Đặt secret key Base32 nhận được từ QR code vào đây
TOTP_SECRET = 'J5MBXLD2IZJSJWADQU'  # FPT Inside TOTP - Phuongnam.phongnh5

# === URL he thong ===
MANAGEMENT_URL = 'https://management.mypt.vn'

# === IMAP Mail FPT (doc OTP tu dong) ===
IMAP_SERVER = 'mail.fpt.net'
IMAP_PORT = 993

# === Google Sheet (Nguồn cũ) ===
SHEET_ID = '17QLc9SlfpPPrR-d1R2BkBxA7DmCI5ZCuhmQffP5F6pY'
SHEET_GID = '1109348771'

# === Supabase Database ===
SUPABASE_URL = 'https://zqkithrewcorqyckfdrr.supabase.co'
SUPABASE_KEY = 'sb_publishable_Tgnpn53W9b0dxDSYCkNXOw_jug0G3ML'

# === Flask ===
FLASK_PORT = 5000
SECRET_KEY = 'mybae_auto_2026'

# === Cache ===
SESSION_FILE = 'session.pkl'

# === Google Sheet Bypass (Hậu Kiểm) ===
BYPASS_SHEET_ID = '1Sw-qSbLzvVdcW4rj7ywEZBEzKI6w5M82EQeYJUpdx7M'
BYPASS_SHEET_GID = '0'
BYPASS_WEBHOOK_URL = 'https://script.google.com/macros/s/AKfycbwKy9963Q4QWjFjyz3l8A8XAu15He15dMU63J-GKu36VjkDaOE1wTaFxfCVEzF8PY09pw/exec'
