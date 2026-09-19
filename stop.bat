@echo off
chcp 65001 >nul
title Stop Twin AI Workbench
echo Stopping Twin AI Workbench (port 7860) ...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":7860" ^| findstr "LISTENING"') do (
    echo   killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

echo Done.
timeout /t 2 >nul
