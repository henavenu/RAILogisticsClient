@echo off
REM ============================================================
REM reset_demo.bat — RAI Trust Platform Demo Reset
REM ============================================================
REM Resets the platform to the pre-onboarding state for the
REM four-act demo narrative:
REM   Act 1: myBank has no governance → Russia transfer PASSES
REM   Act 2: Onboard myBank in PAP → assign sanctions + session_guard packs
REM   Act 3: Russia transfer is BLOCKED (COERCIVE — sanctions)
REM   Act 4: Subsequent transfer attempt BLOCKED (session_guard — trust floor)
REM
REM What this script does:
REM   1. Removes mybank from app_stacks.yaml  (pre-onboarding state)
REM   2. Purges all active sessions            (clean session store)
REM   3. Resets mybank app trust score         (back to 1.0)
REM   4. Restarts Policy Engine                (picks up stacks change)
REM
REM Usage:
REM   reset_demo.bat             (full reset — kills running PE)
REM   reset_demo.bat --no-restart (reset files only, keep PE running)
REM ============================================================

setlocal enabledelayedexpansion

set PE_URL=http://localhost:3000
set MYBANK_URL=http://localhost:8000
set APP_STACKS_FILE=policy-engine\config\app_stacks.yaml
set SKIP_RESTART=0

if "%1"=="--no-restart" set SKIP_RESTART=1

echo.
echo ============================================================
echo  RAI Trust Platform — Demo Reset
echo ============================================================
echo.

REM ── Step 1: Remove mybank from app_stacks.yaml ───────────────
echo [1/4] Removing mybank from app_stacks.yaml...

REM Write a clean app_stacks.yaml with no apps onboarded (pre-onboarding state)
(
echo # App Stacks — RAI Trust Platform
echo # Managed by AppStackManager ^(PAP API^). Last written at runtime.
echo.
echo app_stacks: {}
) > "%APP_STACKS_FILE%"

if errorlevel 1 (
    echo   ERROR: Could not write app_stacks.yaml
    exit /b 1
)
echo   OK — app_stacks cleared ^(no apps onboarded^)

REM ── Step 2: Purge sessions via PE API ────────────────────────
echo.
echo [2/4] Purging active sessions...
curl -s -X POST "%PE_URL%/internal/sessions/purge" -o nul 2>nul
if errorlevel 1 (
    echo   WARNING: Could not reach PE at %PE_URL% — sessions will clear on restart
) else (
    echo   OK — sessions purged
)

REM ── Step 3: Reset mybank server (sessions + balances) ──────────
echo.
echo [3/5] Resetting mybank session state and account balances...
curl -s -X POST "%MYBANK_URL%/api/reset" -o nul 2>nul
if errorlevel 1 (
    echo   WARNING: Could not reach mybank at %MYBANK_URL% — restart mybank server manually
) else (
    echo   OK — mybank sessions cleared, account balances restored to $12,847.33 / $45,200.00
)

REM ── Step 4: Reset mybank app trust score ─────────────────────
echo.
echo [4/5] Resetting mybank app trust score...
curl -s -X DELETE "%PE_URL%/internal/app-trust/mybank" -o nul 2>nul
if errorlevel 1 (
    echo   WARNING: Could not reset app trust (PE may not be running)
) else (
    echo   OK — mybank trust score reset to 1.0
)

REM ── Step 5: Restart Policy Engine ────────────────────────────
echo.
echo [5/5] Restarting Policy Engine...
if "%SKIP_RESTART%"=="1" (
    echo   SKIPPED ^(--no-restart flag set^)
    echo   NOTE: Restart the PE manually to pick up app_stacks.yaml change
) else (
    REM Kill existing PE process on port 3000
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":3000 " ^| findstr "LISTENING"') do (
        taskkill /F /PID %%a >nul 2>&1
    )
    echo   Killed existing PE process
    timeout /t 1 /nobreak >nul

    REM Start PE in background
    start "Policy Engine" /B cmd /c "cd policy-engine && python main.py > logs\startup.log 2>&1"
    echo   PE starting... (logs: policy-engine\logs\startup.log)
    timeout /t 3 /nobreak >nul

    REM Verify PE is up
    curl -s "%PE_URL%/v1/health" -o nul 2>nul
    if errorlevel 1 (
        echo   WARNING: PE health check failed — may still be starting up
    ) else (
        echo   OK — PE is running
    )
)

echo.
echo ============================================================
echo  Demo reset complete.
echo.
echo  Platform state:
echo    mybank:  NOT onboarded (no stack entry)
echo    sessions: cleared
echo    mybank trust score: 1.0
echo.
echo  Four-act demo:
echo    Act 1: Run myBank app, send Russia wire → PASSES (no governance)
echo    Act 2: Open PAP, onboard myBank, assign sanctions + session_guard packs
echo    Act 3: Send Russia wire again → BLOCKED ^(COERCIVE — sanctions^)
echo    Act 4: Send any transfer again → BLOCKED ^(session_guard trust floor^)
echo ============================================================
echo.

endlocal
