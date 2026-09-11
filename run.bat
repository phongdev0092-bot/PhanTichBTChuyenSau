@echo off
title MYBAE AUTO Dashboard
color 0A
cd /d "%~dp0"

echo =====================================================
echo          MYBAE AUTO DASHBOARD - FPT Telecom
echo =====================================================
echo.

if not exist cloudflared.exe (
    echo [*] Dang tai Cloudflare Tunnel (Mien phi 100%%)...
    curl.exe -L -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
)

echo [*] Dang khoi dong Web Dashboard va khoi tao duong link Online truy cap tu xa...
echo [*] Trinh duyet local: http://localhost:5000
echo.

python app.py

if errorlevel 1 (
    echo.
    echo [*] Thong bao: Neu khong chay duoc "python", dang thu bang "py"...
    py app.py
)

pause

