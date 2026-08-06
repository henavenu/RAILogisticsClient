@echo off
REM ── MyBank demo launcher ─────────────────────────────────────────────────
REM Opens MyBank in a frameless browser "app" window: no address bar, no tabs,
REM no visible localhost/port. Operator just double-clicks this file.
set "URL=http://127.0.0.1:5000"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "PROFILE=%TEMP%\rai-demo-mybank"

if exist "%CHROME%" (
  start "" "%CHROME%" --app=%URL% --window-size=1500,950 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
) else (
  start "" "%EDGE%" --app=%URL% --window-size=1500,950 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
)
