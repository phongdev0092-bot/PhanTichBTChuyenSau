@echo off
title MYBAE AUTO Dashboard
color 0A
cd /d "%~dp0"

echo =====================================================
echo          MYBAE AUTO DASHBOARD - FPT Telecom
echo =====================================================
echo.
echo [*] Dang khoi dong Web Dashboard...
echo [*] Trinh duyet se tu dong mo http://localhost:5000
echo.

python app.py

if errorlevel 1 (
    echo.
    echo [*] Thong bao: Neu khong chay duoc "python", dang thu bang "py"...
    py app.py
)

pause
