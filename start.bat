@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Multi-AI Workbench
cd /d "%~dp0"

echo ============================================================
echo   Multi-AI Workbench
echo ============================================================
echo.

set "PYEXE=%~dp0venv\Scripts\python.exe"
if exist "%PYEXE%" goto havepy

echo [info] venv not found - falling back to system python
rem NOTE: the dep probe below MUST use `python` (console build), never `pythonw`.
rem pythonw is a GUI-subsystem binary: cmd does not wait for it, so errorlevel
rem does not reflect its result and the self-check gets mis-judged as failed.
set "PYEXE=python"
python -c "import fastapi,uvicorn" >nul 2>nul
if not errorlevel 1 goto havepy

echo.
echo ============================================================
echo   [ERROR] System Python is missing deps: fastapi / uvicorn
echo ============================================================
echo   Please install them first, then double-click this file again:
echo       python -m pip install -r "%~dp0requirements.txt"
echo.
pause
exit /b 1

:havepy

echo [1/3] Running self-check ...
"%PYEXE%" "%~dp0check.py"
if errorlevel 1 (
    echo.
    echo ============================================================
    echo   SELF-CHECK FAILED - fatal problem found
    echo ============================================================
    echo    NOTE: if it is only a wrong model name / API key it is probably a
    echo    false alarm - you can start anyway and fix it inside the app.
    echo.
    choice /c YN /t 20 /d N /m "  Start anyway? [Y=yes / N=quit, auto-quit in 20s]"
    if errorlevel 2 exit /b 1
    echo   [info] continuing anyway ...
    echo.
)
echo.

echo [2/3] Checking port 7860 ...
netstat -ano | findstr ":7860" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [info] Port 7860 already listening - server is probably running.
    start "" http://localhost:7860
    echo.
    pause
    exit /b 0
)

echo [3/3] Starting server ...
start "MultiAI-Server" "%PYEXE%" "%~dp0app.py"

set /a waited=0
:wait
rem timeout needs a real console: when stdin is redirected it fails instantly
rem (20 iterations in 0s -> false "did not start in 20s"). ping is the fallback.
timeout /t 1 /nobreak >nul 2>&1 || ping -n 2 127.0.0.1 >nul 2>&1
set /a waited+=1
netstat -ano | findstr ":7860" | findstr "LISTENING" >nul
if not errorlevel 1 goto up
if %waited% GEQ 20 goto timeout
goto wait

:up
echo.
echo ============================================================
echo   Started!  Opening http://localhost:7860
echo   Closing this window will NOT stop the server.
echo   To stop the server, run stop.bat
echo ============================================================
start "" http://localhost:7860
echo.
pause
exit /b 0

:timeout
echo.
echo [ERROR] Server did not start listening on 7860 within 20s.
echo         Please check the minimized server window for errors.
echo.
pause
exit /b 1
