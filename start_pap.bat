@echo off
REM RAI Trust Platform - Start PAP only (port 3002)
REM NOTE: Policy Engine (3000) and Guardrail Service (3001) should be running.

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo venv not found. Run start_all.bat first to create it.
    pause & exit /b 1
)

echo Starting PAP on port 3002...
echo NOTE: Policy Engine (3000) and Guardrail Service (3001) should be running.
start "RAI - PAP [3002]" /d "%~dp0pap" cmd /k _run.bat

echo PAP starting at http://localhost:3002/ui
pause
