@echo off
REM RAI Assurance Service - Start
REM Standalone launcher. Independent of the Trust Platform repo's venv.
REM Default provider is "mock" so the service runs without Arize credentials.

echo ============================================================
echo  RAI Assurance Service - Starting
echo ============================================================
echo.

REM Work from the script's own directory regardless of where it is called from
cd /d "%~dp0"

REM ---- Check / create virtual environment ----------------------
if exist "venv\Scripts\activate.bat" (
    echo [1/2] Virtual environment found.
) else (
    echo [1/2] Virtual environment not found. Creating it now...
    echo       This only happens once and may take a few minutes.
    echo.
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo  ERROR: Could not create venv. Is Python on PATH?
        echo  Test with:  python --version
        pause
        exit /b 1
    )
    call venv\Scripts\activate.bat
    echo.
    echo       Installing Assurance dependencies ^(pyproject.toml^)...
    pip install -e ".[dev]"
    if errorlevel 1 (
        echo.
        echo  ERROR: pip install failed. Check pyproject.toml and internet connection.
        pause
        exit /b 1
    )
    echo.
    echo  Setup complete!
    echo.
)

REM ---- Provider selection --------------------------------------
REM   mock  - returns synthetic span data, no external deps. Safe default.
REM   arize - real Arize integration. Requires arize package + ARIZE_API_KEY + ARIZE_SPACE_ID.
REM   local - reads spans from data/spans.jsonl.
REM
REM To run against real Arize:
REM   set ASSURANCE_PROVIDER=arize
REM   set ARIZE_API_KEY=...
REM   set ARIZE_SPACE_ID=...
REM   start_assurance.bat
if not defined ASSURANCE_PROVIDER set "ASSURANCE_PROVIDER=mock"

echo.
echo [2/2] Starting Assurance Service on port 8000 (provider=%ASSURANCE_PROVIDER%)...
echo.
echo ============================================================
echo  Endpoints:
echo    Swagger UI : http://127.0.0.1:8000/v1/docs
echo    Health     : http://127.0.0.1:8000/v1/healthz
echo    Catalog    : http://127.0.0.1:8000/v1/catalog
echo    Metrics    : http://127.0.0.1:8000/v1/metrics?app_id=mybank^&window=24h
echo ============================================================
echo.
echo  Ctrl+C to stop.
echo.

call venv\Scripts\activate.bat
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
