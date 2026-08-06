@echo off
REM Mock Assurance Provider — port 3010
REM Called by start_all.bat from the pap\tools\proxy_assurance directory.
REM %~dp0 is always this file's own directory, regardless of how it was invoked.

cd /d "%~dp0"

REM Root venv is three levels up: pap\tools\proxy_assurance → pap\tools → pap → root
set "_VENV=%~dp0..\..\..\venv\Scripts\activate.bat"
if not exist "%_VENV%" (
    echo [Proxy] ERROR: root venv not found at %_VENV%
    echo [Proxy] Run start_all.bat first to create the venv.
    pause & exit /b 1
)

call "%_VENV%"
echo [Proxy] Starting Mock Assurance Provider on port 3010...
uvicorn proxy_assurance:app --host 0.0.0.0 --port 3010 --log-level info
pause
