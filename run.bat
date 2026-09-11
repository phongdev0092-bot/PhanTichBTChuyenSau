@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
title MYBAE AUTO Dashboard
color 0A
cd /d "%~dp0"

echo =====================================================
echo          MYBAE AUTO DASHBOARD - FPT Telecom
echo =====================================================
echo.

if not exist cloudflared.exe (
    echo [*] Dang tai Cloudflare Tunnel...
    curl.exe -L -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
)

echo [*] Dang khoi dong Web Dashboard...
echo [*] Trinh duyet local: http://localhost:5000
echo.

python app.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [*] Neu khong chay duoc "python", dang thu bang "py"...
    py app.py
)

pause
