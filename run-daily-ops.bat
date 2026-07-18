@echo off
REM Shadow Edge — morning Daily Ops refresh.
REM Registered as the scheduled task "ShadowEdge-DailyOps-Morning".
REM   1) listening scan — find + draft engagement replies (LLM; never posts)
REM   2) daily_posts    — draft today's platform-native social posts (LLM)
REM   3) video_script   — draft the next-video script from the winning theme (LLM)
REM   4) ops_tracker    — regenerate daily-ops\<today>.md from current state (offline)
REM so the reply drafts, posts, script, and brief are all waiting on /today.
setlocal
cd /d "%~dp0"
set PYTHONNOUSERSITE=1
set PYTHONHOME=
set UV_INTERNAL__PYTHONHOME=

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" set "VENV_PY=python"

if not exist "%~dp0state\logs" mkdir "%~dp0state\logs"
echo [%date% %time%] daily-ops refresh >> "%~dp0state\logs\daily-ops.log"
"%VENV_PY%" scripts\listening.py scan >> "%~dp0state\logs\daily-ops.log" 2>&1
echo [%date% %time%] scan exit=%ERRORLEVEL% >> "%~dp0state\logs\daily-ops.log"
"%VENV_PY%" scripts\daily_posts.py generate >> "%~dp0state\logs\daily-ops.log" 2>&1
echo [%date% %time%] posts exit=%ERRORLEVEL% >> "%~dp0state\logs\daily-ops.log"
"%VENV_PY%" scripts\video_script.py >> "%~dp0state\logs\daily-ops.log" 2>&1
echo [%date% %time%] script exit=%ERRORLEVEL% >> "%~dp0state\logs\daily-ops.log"
"%VENV_PY%" scripts\ops_tracker.py >> "%~dp0state\logs\daily-ops.log" 2>&1
echo [%date% %time%] brief exit=%ERRORLEVEL% >> "%~dp0state\logs\daily-ops.log"
endlocal
