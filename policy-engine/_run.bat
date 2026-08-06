@echo off
REM Launcher for Policy Engine — called by start_all.bat or directly.
REM %~dp0 is always this file's directory (policy-engine\)

cd /d "%~dp0"

REM Pin the shared DB path so it cannot be overridden by an inherited env var
REM from a parent process (e.g. start_all.bat set it to a stale value).
REM load_dotenv() in main.py skips vars already in the environment, so
REM we set it here explicitly to guarantee the correct file is used.
set "AUDIT_SQLITE_PATH=%~dp0..\pap\config\pap.db"

call "%~dp0..\venv\Scripts\activate.bat"
echo [PE] AUDIT_SQLITE_PATH=%AUDIT_SQLITE_PATH%
echo [PE] Starting Policy Engine on port 3003...
python main.py
pause
