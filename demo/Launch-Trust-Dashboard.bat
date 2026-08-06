@echo off
REM ── Trust Platform dashboard demo launcher ───────────────────────────────
REM Opens the PAP dashboard in a frameless browser "app" window: no address
REM bar, no tabs, no visible localhost/port. Operator just double-clicks it.
set "URL=http://127.0.0.1:3002/ui/dashboard"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "PROFILE=%TEMP%\rai-demo-dashboard"

if exist "%CHROME%" (
  start "" "%CHROME%" --app=%URL% --window-size=1600,1000 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
) else (
  start "" "%EDGE%" --app=%URL% --window-size=1600,1000 --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check
)
