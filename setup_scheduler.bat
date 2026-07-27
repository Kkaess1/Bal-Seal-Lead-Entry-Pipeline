@echo off
:: ============================================================================
:: Lead Input Pipeline - Windows Task Scheduler Setup
:: 
:: Schedules:
::   1. Lead Ingestion  (pipeline_ingest.py) -> 8:00 AM & 4:00 PM daily
::   2. Oracle Submit   (pipeline_submit.py) -> 5:00 PM daily
:: ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

echo ============================================================================
echo Registering Lead Input Pipeline Scheduled Tasks...
echo ============================================================================
echo.

:: 1. Ingestion Job - Morning (8:00 AM)
schtasks /create /tn "LeadPipeline_Ingest_AM" ^
    /tr "\"%PYTHON_EXE%\" \"%SCRIPT_DIR%pipeline_ingest.py\"" ^
    /sc daily /st 08:00 /f
if %errorlevel% equ 0 (
    echo [OK] Registered Morning Ingest Task: 8:00 AM Daily
) else (
    echo [ERROR] Failed to register Morning Ingest Task
)

:: 2. Ingestion Job - Afternoon (4:00 PM)
schtasks /create /tn "LeadPipeline_Ingest_PM" ^
    /tr "\"%PYTHON_EXE%\" \"%SCRIPT_DIR%pipeline_ingest.py\"" ^
    /sc daily /st 16:00 /f
if %errorlevel% equ 0 (
    echo [OK] Registered Afternoon Ingest Task: 4:00 PM Daily
) else (
    echo [ERROR] Failed to register Afternoon Ingest Task
)

:: 3. Oracle Submission Job - Evening (5:00 PM)
schtasks /create /tn "LeadPipeline_Submit_OSC" ^
    /tr "\"%PYTHON_EXE%\" \"%SCRIPT_DIR%pipeline_submit.py\"" ^
    /sc daily /st 17:00 /f
if %errorlevel% equ 0 (
    echo [OK] Registered OSC Submission Task: 5:00 PM Daily
) else (
    echo [ERROR] Failed to register OSC Submission Task
)

echo.
echo ============================================================================
echo Task Scheduler Setup Complete!
echo Tasks registered:
echo   - LeadPipeline_Ingest_AM (8:00 AM Daily)
echo   - LeadPipeline_Ingest_PM (4:00 PM Daily)
echo   - LeadPipeline_Submit_OSC (5:00 PM Daily)
echo ============================================================================
pause
