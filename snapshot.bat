@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM Take a CONFIG SNAPSHOT of the live PAP database.
REM
REM Run this MANUALLY whenever you've made policy / threshold / app-config edits
REM (via the PAP admin UI) that you want to ship and commit. It snapshots pap.db's
REM config_* tables into pap/config/pap.seed.db (excludes all audit/log data) and
REM updates the local apply-marker so your next start won't re-seed your dev DB.
REM
REM This is fully decoupled from git — committing does NOT auto-snapshot. After
REM running this, review + commit pap\config\pap.seed.db to ship the config change.
REM ─────────────────────────────────────────────────────────────────────────────
cd /d "%~dp0"
python tools\make_seed_db.py
if errorlevel 1 py tools\make_seed_db.py
echo.
echo Snapshot complete. Commit pap\config\pap.seed.db to ship these config changes.
