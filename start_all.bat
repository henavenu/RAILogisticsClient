@echo off
REM RAI Trust Platform - Start All Services
REM
REM Service map:
REM   3001  Guardrail Service
REM   3002  PAP (Policy Admin Platform + Web UI + Dashboard)
REM   3003  Policy Engine
REM   3010  Mock Assurance Provider  (launched only when ASSURANCE_URL=127.0.0.1:3010)
REM   3012  Sentinel Agent           (cross-turn blind-spot detection)
REM   3013  Coordinator Agent        (recommendation layer)
REM   5000  myBank Application

cd /d "%~dp0"

REM -- Fresh-clone bootstrap: create missing .env files from .env.example ---------
REM  A clone has only .env.example (the real .env is gitignored). Without a .env,
REM  services that REQUIRE SENTINEL_URL / TRUST_PE_URL / PAP_URL / etc. fail closed
REM  and myBank never loads (blank dashboard). Copy any missing one so the stack can
REM  boot. The URLs/ports come pre-filled from the example; you must still add your
REM  secret keys (GROQ_API_KEY, or GOOGLE_CLOUD_PROJECT for Vertex) in the new .env.
set "_ENV_CREATED="
for %%D in ("." "pap" "policy-engine" "guardrail-service" "sdk\examples\mybank_clean" "trust-agents" "trust-agents\sentinel" "trust-agents\coordinator") do (
  if exist "%%~D\.env.example" if not exist "%%~D\.env" (
    copy /Y "%%~D\.env.example" "%%~D\.env" >nul
    echo [setup] created %%~D\.env from .env.example
    set "_ENV_CREATED=1"
  )
)
if defined _ENV_CREATED (
  echo.
  echo [setup] ^>^>^> New .env files were created from templates.
  echo [setup]     Add your secret keys ^(GROQ_API_KEY, etc.^) to those .env files,
  echo [setup]     then re-run this script. Services run now, but LLM calls need a key.
  echo.
)

REM -- DB init: materialise pap.db from the committed snapshot -------------------
REM  Single source of init - the SAME tools\seed_local.py the Docker entrypoint runs,
REM  so a fresh clone, a git pull, and a container start behave IDENTICALLY. It
REM  re-seeds pap.db from pap.seed.db when the snapshot changed (clone OR pull) and
REM  preserves pap.db on a plain restart. The snapshot is the schema+config source of
REM  truth - we no longer rely on init_db()/alembic to build the schema (which drifts
REM  and caused the "no column content_snippet" errors on fresh pulls).
python tools\seed_local.py
if errorlevel 1 py tools\seed_local.py
if errorlevel 1 echo [setup] WARNING: DB init failed - is python on PATH?

REM -- Correctness fixes (no user edits required) --------------------------------
REM  AUDIT_SQLITE_PATH: absolute path to the shared PAP database (rai_trust.db).
REM    Without this, the Policy Engine runs in JSONL-only mode and the PAP
REM    dashboard shows no evaluations or alerts.
REM  Service URLs: pin to 127.0.0.1 (not localhost) to avoid the Windows IPv6
REM    ~2 s stall that occurs before fallback to IPv4.
set "AUDIT_SQLITE_PATH=%~dp0pap\config\pap.db"
set "GUARDRAIL_SERVICE_URL=http://127.0.0.1:3001"
set "POLICY_ENGINE_URL=http://127.0.0.1:3003"

REM -- Load root .env (user-tunable runtime config) -----------------------------
REM  Reads key=value pairs; lines starting with # are skipped.
REM  Service-level .env files are loaded by each service's Python startup and
REM  OVERRIDE these values for that process.
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if not "%%a"=="" set "%%a=%%b"
  )
)

REM -- Determine whether to launch the mock Assurance Provider ------------------
REM  Proxy launches automatically when ASSURANCE_URL points at the local mock.
REM  Set ASSURANCE_URL to a real provider URL in .env to skip the proxy.
if not defined ASSURANCE_URL set "ASSURANCE_URL=http://127.0.0.1:3010"
set "_LAUNCH_PROXY=0"
if /I "%ASSURANCE_URL%"=="http://127.0.0.1:3010" set "_LAUNCH_PROXY=1"
if /I "%ASSURANCE_URL%"=="http://localhost:3010"  set "_LAUNCH_PROXY=1"

echo ============================================================
echo  RAI Trust Platform - Starting All Services
echo ============================================================
echo.

REM -- Check virtual environment ------------------------------------------------
if exist "venv\Scripts\activate.bat" (
    echo [Setup] Virtual environment found.
) else (
    echo [Setup] Virtual environment not found. Creating it now...
    python -m venv venv
    if errorlevel 1 (
        echo  ERROR: Could not create venv. Is Python on PATH?
        pause & exit /b 1
    )
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
    if errorlevel 1 (
        echo  ERROR: pip install failed.
        pause & exit /b 1
    )
)

REM -- Pre-flight: release stale sockets on all service ports -------------------
REM Kills processes holding a service port in ANY TCP state (Listen, Established,
REM CloseWait, etc.).  Using -State Listen alone misses sockets stuck in
REM CloseWait/Established after a previous process was force-killed, which causes
REM WinError 10048 when uvicorn tries to rebind.
REM OwningProcess -gt 4 skips kernel/System entries (PID 0 and 4).
echo.
echo [Pre-flight] Releasing stale sockets on service ports...
powershell -NoProfile -Command "$ports = @(3001,3002,3003,3010,3012,3013,5000); foreach ($p in $ports) { $pids = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -gt 4 } | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($procId in $pids) { Write-Host ('[Pre-flight] Killing PID ' + $procId + ' on port ' + $p); Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } }"
timeout /t 3 /nobreak >nul
echo [Pre-flight] Done.
echo.

REM -- Mock Assurance Provider (optional) ---------------------------------------
if "%_LAUNCH_PROXY%"=="1" (
  if exist "%~dp0pap\tools\proxy_assurance\start_proxy.bat" (
    echo [0/5] Starting Mock Assurance Provider on port 3010...
    start "RAI - Mock Assurance [3010]" /d "%~dp0pap\tools\proxy_assurance" cmd /k "%~dp0pap\tools\proxy_assurance\start_proxy.bat"
    timeout /t 3 /nobreak >nul
  ) else (
    echo [0/5] Mock Assurance Provider not found at pap\tools\proxy_assurance\ -- skipping.
  )
)

REM -- UI / Admin cluster --------------------------------------------------------
echo [1/5] Starting PAP (Policy Admin + Dashboard) on port 3002...
start "RAI - PAP [3002]" /d "%~dp0pap" cmd /k "%~dp0pap\_run.bat"
timeout /t 3 /nobreak >nul

REM -- Governance cluster --------------------------------------------------------
REM  Guardrail Service does not read the PAP DB - starts in parallel while PAP boots.
echo [2/5] Starting Guardrail Service on port 3001...
start "RAI - Guardrail Service [3001]" /d "%~dp0guardrail-service" cmd /k "%~dp0guardrail-service\_run.bat"

REM -- Wait for PAP before Policy Engine -----------------------------------------
REM  PAP runs init_db() + seed_all() before uvicorn binds port 3002, so a listening
REM  socket on :3002 is a guarantee that config_policies is seeded.
REM  Without this wait, PE starts while the DB has no tables (race condition) and
REM  aborts with "no such table: config_policies".
echo [Wait] Waiting for PAP to finish DB init (port 3002, up to 60 s)...
powershell -NoProfile -Command "$limit = 60; $n = 0; do { if (Get-NetTCPConnection -LocalPort 3002 -State Listen -ErrorAction SilentlyContinue) { Write-Host '[Wait] PAP ready.'; $n = $limit + 1; break } Start-Sleep -Seconds 2; $n += 2; Write-Host ('[Wait] ' + $n + 's - PAP not yet on :3002...') } while ($n -lt $limit); if ($n -eq $limit) { Write-Host '[Wait] WARNING: PAP did not come up in 60 s - proceeding anyway' }"

echo [3/5] Starting Policy Engine on port 3003...
start "RAI - Policy Engine [3003]" /d "%~dp0policy-engine" cmd /k "%~dp0policy-engine\_run.bat"
timeout /t 4 /nobreak >nul

REM -- Trust Agents cluster ------------------------------------------------------
echo [4/5] Starting Sentinel Agent on port 3012...
start "RAI - Sentinel [3012]" /d "%~dp0trust-agents\sentinel" cmd /k "%~dp0trust-agents\sentinel\_run.bat"
timeout /t 3 /nobreak >nul

echo [5/5] Starting Coordinator Agent on port 3013...
start "RAI - Coordinator [3013]" /d "%~dp0trust-agents\coordinator" cmd /k "%~dp0trust-agents\coordinator\_run.bat"
timeout /t 3 /nobreak >nul

REM -- myBank Application --------------------------------------------------------
echo [+] Starting myBank Application on port 5000...
start "RAI - myBank [5000]" /d "%~dp0sdk\examples\mybank_clean" cmd /k "%~dp0sdk\examples\mybank_clean\_run.bat"
timeout /t 3 /nobreak >nul

REM -- Verify all ports came up --------------------------------------------------
echo.
echo [Verify] Checking all service ports...
powershell -NoProfile -Command "$ports = @(3001,3002,3003,3010,3012,3013,5000); $names = @{3001='Guardrail';3002='PAP';3003='Policy Engine';3010='Mock Assurance';3012='Sentinel';3013='Coordinator';5000='myBank'}; foreach ($p in $ports) { $up = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Where-Object { $_.State -in 'Listen','Established' } | Select-Object -First 1; if ($up) { Write-Host ('[UP  ] :' + $p + '  ' + $names[$p]) } else { Write-Host ('[WAIT] :' + $p + '  ' + $names[$p] + ' (still starting - check window)') } }"

echo.
echo ============================================================
echo  All services started!
echo.
echo  UI / Admin
echo    PAP + Dashboard  : http://127.0.0.1:3002/ui
echo    PAP API docs     : http://127.0.0.1:3002/docs
echo.
echo  Governance
echo    Guardrail Svc    : http://127.0.0.1:3001/docs
echo    Policy Engine    : http://127.0.0.1:3003/docs
echo.
echo  Trust Agents
echo    Sentinel Agent   : http://127.0.0.1:3012/docs
echo    Coordinator      : http://127.0.0.1:3013/docs
echo    Mock Assurance   : http://127.0.0.1:3010/v1/docs  (if _LAUNCH_PROXY=1)
echo.
echo  Applications
echo    myBank           : http://127.0.0.1:5000
echo.
echo  Dashboard
echo    Trust Radar      : http://127.0.0.1:3002/ui/dashboard
echo.
echo  Logs
echo    guardrail-service\logs\guardrail-service.log
echo    policy-engine\logs\policy-engine.log
echo    pap\logs\pap.log
echo.
echo  Audit trail: policy-engine\logs\audit.jsonl
echo ============================================================
echo.
pause
