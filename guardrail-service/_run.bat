@echo off
REM Launcher for Guardrail Service - called by start_all.bat
REM %~dp0 is always this file's directory (guardrail-service\)
call "%~dp0..\venv\Scripts\activate.bat"
python main.py
pause
