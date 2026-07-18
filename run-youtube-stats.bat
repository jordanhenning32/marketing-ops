@echo off
REM Shadow Edge — daily YouTube stats pull (feedback loop).
REM Registered as the scheduled task "ShadowEdge-YouTube-Stats-Daily".
REM Uses the repo-local venv so PATH cannot select another app's Python
REM (the system/uv python on this machine is missing the Google API deps).
setlocal
cd /d "%~dp0"
set PYTHONNOUSERSITE=1
set PYTHONHOME=
set UV_INTERNAL__PYTHONHOME=

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" set "VENV_PY=python"

if not exist "%~dp0state\logs" mkdir "%~dp0state\logs"
echo [%date% %time%] youtube_stats pull >> "%~dp0state\logs\youtube-stats.log"
"%VENV_PY%" scripts\youtube_stats.py pull >> "%~dp0state\logs\youtube-stats.log" 2>&1
echo [%date% %time%] pull exit=%ERRORLEVEL% >> "%~dp0state\logs\youtube-stats.log"
REM GA4 website analytics pull (visitors / page views / top page -> scoreboard).
echo [%date% %time%] ga4 pull >> "%~dp0state\logs\youtube-stats.log"
"%VENV_PY%" scripts\ga4_stats.py pull >> "%~dp0state\logs\youtube-stats.log" 2>&1
echo [%date% %time%] ga4 exit=%ERRORLEVEL% >> "%~dp0state\logs\youtube-stats.log"
REM Re-derive the editor's brief + guidance from the fresh numbers so the next
REM video is steered by the latest results.
"%VENV_PY%" scripts\refresh_editor.py >> "%~dp0state\logs\youtube-stats.log" 2>&1
echo [%date% %time%] editor exit=%ERRORLEVEL% >> "%~dp0state\logs\youtube-stats.log"
REM Keep /today aligned with the refreshed stats and editor guidance.
"%VENV_PY%" scripts\ops_tracker.py >> "%~dp0state\logs\youtube-stats.log" 2>&1
echo [%date% %time%] daily ops exit=%ERRORLEVEL% >> "%~dp0state\logs\youtube-stats.log"
endlocal
