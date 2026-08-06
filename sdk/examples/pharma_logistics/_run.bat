@echo off
REM myBank Application — port 5000
REM Called by start_all.bat from the sdk\examples\mybank_clean directory.
REM %~dp0 is always this file's own directory, regardless of how it was invoked.

cd /d "%~dp0"

REM Pin service URLs so inherited env vars from start_all.bat cannot override them.
set "MYBANK_CALLBACK_URL=http://127.0.0.1:5000"
set "PAP_URL=http://127.0.0.1:3002"
set "SENTINEL_URL=http://127.0.0.1:3012"

REM mybank_clean has its own venv. The app itself is NOT a package (no pyproject.toml),
REM so its deps = its PyPI requirements.txt PLUS the three in-repo packages from the repo
REM root (privacy-proxy, sdk, sdk-langgraph) — the same set the Dockerfile installs.
REM First run auto-provisions it (same pattern start_all.bat uses for the root venv);
REM later runs skip straight to launch.
set "_VENV=%~dp0venv\Scripts\activate.bat"
if not exist "%_VENV%" (
    echo [myBank] First run — creating local venv and installing dependencies ^(one-time, ~1-2 min^)...
    python -m venv "%~dp0venv"
    if errorlevel 1 (
        echo [myBank] ERROR: could not create venv. Is Python on PATH?
        pause & exit /b 1
    )
    call "%_VENV%"
    python -m pip install --upgrade pip --quiet
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [myBank] ERROR: 'pip install -r requirements.txt' failed. See output above.
        pause & exit /b 1
    )
    REM In-repo packages from the repo root. Order matters: sdk before sdk-langgraph
    REM (which depends on rai-trust-sdk). One command so pip resolves the local deps.
    pip install "%~dp0..\..\..\privacy-proxy" "%~dp0..\..\..\sdk" "%~dp0..\..\..\sdk-langgraph"
    if errorlevel 1 (
        echo [myBank] ERROR: failed to install in-repo packages ^(privacy-proxy/sdk/sdk-langgraph^). See output above.
        pause & exit /b 1
    )
) else (
    call "%_VENV%"
)
echo [myBank] Starting myBank Application on port 5000...
python server.py
pause
