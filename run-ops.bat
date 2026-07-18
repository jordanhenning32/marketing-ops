@echo off
REM Run the Operations Tracker once (silent, fast).
REM Useful for Windows Task Scheduler to fire daily at 7 AM ET.

cd /d "%~dp0"
python scripts\ops_tracker.py
