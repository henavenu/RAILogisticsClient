@echo off
REM RAI Trust Platform - Start Guardrail Service only (port 3001)

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo venv not found. Run start_all.bat first to create it.
    pause & exit /b 1
)

echo Starting Guardrail Service on port 3001...
start "RAI - Guardrail Service [3001]" /d "%~dp0guardrail-service" cmd /k _run.bat

echo Guardrail Service starting at http://localhost:3001/docs
pause
