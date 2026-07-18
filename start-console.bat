@echo off
REM Shadow Edge Ops Console launcher
REM 1. Opens splash.html immediately (browser tab with branded boot animation)
REM 2. Installs deps quietly (fast no-op after first run)
REM 3. Boots FastAPI on http://localhost:8876 (no extra browser window;
REM    the splash polls /console-id and auto-redirects when ready)

cd /d "%~dp0"

REM Use a Shadow Edge specific port. Port 8765 is commonly used by the
REM futures-bot operator server on this machine.
if not defined CONSOLE_PORT set CONSOLE_PORT=8876

REM Open the splash page right away so the user sees something immediately.
start "" "%~dp0static\splash.html"

REM Use a repo-local virtualenv so PATH cannot accidentally select another app's Python.
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    where uv >nul 2>nul
    if not errorlevel 1 (
        uv venv "%~dp0.venv" --python 3.11
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo [Shadow Edge] Python not found on PATH. Install Python 3.11+ and try again.
            pause
            exit /b 1
        )
        python -m venv "%~dp0.venv"
    )
)

if not exist "%VENV_PY%" (
    echo [Shadow Edge] Could not create .venv. Install Python 3.11+ or uv and try again.
    pause
    exit /b 1
)

REM Install deps quietly (fast no-op after first run; prints only on failure)
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [Shadow Edge] Dependency install failed. See the error above.
    pause
    exit /b 1
)

echo [Shadow Edge] Console will open at http://127.0.0.1:%CONSOLE_PORT%/today

REM Free the port first: stop any stale console still listening on it, so this
REM launch can bind instead of failing with "only one usage of each socket
REM address" and leaving the old (out-of-date) instance running.
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %CONSOLE_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 1 /nobreak >nul

REM Boot the console. --no-browser because the splash handles the redirect.
"%VENV_PY%" scripts\console.py --no-browser

REM If uvicorn exits, give the user a chance to read any error before the window closes.
pause
