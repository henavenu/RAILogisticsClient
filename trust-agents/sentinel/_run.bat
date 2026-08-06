@echo off
REM Sentinel Agent — port 3012
REM Called by start_all.bat from the trust-agents\sentinel directory.
REM %~dp0 is always this file's own directory, regardless of how it was invoked.
call "%~dp0..\..\venv\Scripts\activate.bat"
python "%~dp0main.py"
pause
