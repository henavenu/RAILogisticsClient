@echo off
REM RAI Trust Platform - Start Policy Engine only (port 3000)
REM NOTE: Guardrail Service (port 3001) must already be running.

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo venv not found. Run start_all.bat first to create it.
    pause & exit /b 1
)

echo Starting Policy Engine on port 3000...
echo NOTE: Guardrail Service must be running on port 3001 first.
start "RAI - Policy Engine [3000]" /d "%~dp0policy-engine" cmd /k _run.bat

echo Policy Engine starting at http://localhost:3000/docs
pause
