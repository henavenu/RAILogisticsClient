@echo off
REM Trust Agents Service — port 3005
REM Run from the trust-agents directory (start_all.bat handles this)

cd /d "%~dp0"
call ..\venv\Scripts\activate.bat

REM Force UTF-8 output so Unicode chars in log messages don't crash on Windows console
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM ── Pre-flight: release port 3005 if any process is holding it ──────────────
REM Kill processes in ANY TCP state (Listen, Established, CloseWait, etc.).
REM The -State Listen filter misses stale CloseWait/Established sockets which
REM can still block a new bind even after the previous process was killed.
echo [Trust Agents] Checking port 3005...
powershell -NoProfile -Command ^
  "Get-NetTCPConnection -LocalPort 3005 -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -gt 4 } | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('[Trust Agents] Releasing port 3005 (PID ' + $_ + ')'); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
REM Give the OS two seconds to fully release the socket
timeout /t 2 /nobreak >nul

echo [Trust Agents] Starting on port 3005...
echo [Trust Agents] LLM: %LLM_BASE_URL% / %LLM_MODEL%
echo [Trust Agents] Schedule: every %SNAPSHOT_INTERVAL_MINUTES% minutes
echo [Trust Agents] DB: %TA_DB_PATH%
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 3005 --log-level info --timeout-graceful-shutdown 0
