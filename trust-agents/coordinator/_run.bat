@echo off
REM Coordinator Agent — port 3013
REM Called by start_all.bat from the trust-agents\coordinator directory.
REM %~dp0 is always this file's own directory, regardless of how it was invoked.
call "%~dp0..\..\venv\Scripts\activate.bat"
python "%~dp0main.py"
pause
