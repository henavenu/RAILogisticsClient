@echo off
REM RAI Trust Platform — Stop All Services
REM
REM Kills every process holding a service port in ANY TCP state.
REM Uses PowerShell (same approach as start_all.bat pre-flight) for reliability.
REM
REM Service map:
REM   3001  Guardrail Service
REM   3002  PAP (Policy Admin + Web UI + Dashboard)
REM   3003  Policy Engine
REM   3010  Mock Assurance Provider
REM   3012  Sentinel Agent
REM   3013  Coordinator Agent
REM   5000  myBank Application

cd /d "%~dp0"

echo ============================================================
echo  RAI Trust Platform - Stopping All Services
echo ============================================================
echo.

powershell -NoProfile -Command ^
  "$ports = @(3001,3002,3003,3010,3012,3013,5000); " ^
  "$names = @{3001='Guardrail';3002='PAP';3003='Policy Engine';3010='Mock Assurance';3012='Sentinel';3013='Coordinator';5000='myBank'}; " ^
  "foreach ($p in $ports) { " ^
  "  $pids = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -gt 4 } | Select-Object -ExpandProperty OwningProcess -Unique; " ^
  "  if ($pids) { " ^
  "    foreach ($procId in $pids) { " ^
  "      Write-Host ('  Stopping ' + $names[$p] + ' [:' + $p + ']  PID=' + $procId); " ^
  "      Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue " ^
  "    } " ^
  "  } else { " ^
  "    Write-Host ('  ' + $names[$p] + ' [:' + $p + '] already stopped') " ^
  "  } " ^
  "}"

echo.
echo Waiting for sockets to release...
timeout /t 3 /nobreak >nul

REM Verify all ports are now free
echo.
echo Verifying ports are free:
powershell -NoProfile -Command ^
  "$ports = @(3001,3002,3003,3010,3012,3013,5000); " ^
  "$names = @{3001='Guardrail';3002='PAP';3003='Policy Engine';3010='Mock Assurance';3012='Sentinel';3013='Coordinator';5000='myBank'}; " ^
  "foreach ($p in $ports) { " ^
  "  $still = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -gt 4 }; " ^
  "  if ($still) { Write-Host ('  [STILL HELD] :' + $p + '  ' + $names[$p]) } " ^
  "  else         { Write-Host ('  [FREE]       :' + $p + '  ' + $names[$p]) } " ^
  "}"

echo.
echo ============================================================
echo  All RAI Trust Platform services stopped.
echo ============================================================
echo.
pause
