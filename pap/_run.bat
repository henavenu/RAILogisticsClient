@echo off
REM Launcher for PAP - called by start_all.bat
REM %~dp0 is always this file's directory (pap\)
call "%~dp0..\venv\Scripts\activate.bat"
python main.py
pause
