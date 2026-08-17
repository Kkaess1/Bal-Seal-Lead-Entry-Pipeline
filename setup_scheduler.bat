@echo off
:: ============================================================================
:: Lead Input Pipeline - Windows Task Scheduler Setup
::
:: Registers ONE task:
::   Review Dashboard (dashboard\serve.py) -> at logon, kept running
::
:: Nothing else runs on a timer, by design:
::
::   * Ingestion is manual. Emails are read when someone clicks "Click to
::     ingest leads" on the Pending Review page. The 8:45 AM and 4:00 PM
::     LeadPipeline_Ingest_AM/PM tasks were removed on 2026-08-07; this script
::     deletes them if an older run registered them.
::
::   * Oracle submission is not scheduled either. A lead reaches Oracle only
::     when a human approves it in the dashboard, which starts
::     pipeline_submit.py itself. The 5:00 PM LeadPipeline_Submit_OSC task was
::     removed the same day and is likewise deleted here.
::
:: The dashboard runs as the signed-in user, not SYSTEM, and only while that
:: user is logged on -- it drives Outlook through COM (reply/forward/delete,
:: and the ingest runs it launches), which needs an interactive session.
:: ============================================================================

setlocal
set "SCRIPT_DIR=%~dp0"

echo ============================================================================
echo Registering Lead Input Pipeline Scheduled Tasks...
echo ============================================================================
echo.

:: 1. Review Dashboard - at logon, restart-on-failure, and a ten-minute
:: heartbeat. setup_dashboard_task.ps1 also pins IgnoreNew so a heartbeat can
:: never create a second server.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup_dashboard_task.ps1"
if %errorlevel% equ 0 (
    echo [OK] Registered Review Dashboard: starts at logon
) else (
    echo [ERROR] Failed to register Review Dashboard task
)

:: 2. Remove the retired timers, if an older run of this script registered them.
for %%T in (LeadPipeline_Ingest_AM LeadPipeline_Ingest_PM LeadPipeline_Submit_OSC) do (
    schtasks /query /tn "%%T" >nul 2>&1
    if not errorlevel 1 (
        schtasks /delete /tn "%%T" /f >nul 2>&1
        echo [OK] Removed retired scheduled task: %%T
    )
)

echo.
echo ============================================================================
echo Task Scheduler Setup Complete!
echo Tasks registered:
echo   - LeadPipeline_Dashboard (at logon, restarted if it stops)
echo.
echo Nothing runs on a timer. Emails are ingested by clicking "Click to ingest
echo leads" on Pending Review, and leads reach Oracle by approving them.
echo ============================================================================
pause
