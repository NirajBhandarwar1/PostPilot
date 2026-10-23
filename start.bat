@echo off
REM Starts PostPilot and keeps it running. Close this window to stop it.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m postpilot run
pause
