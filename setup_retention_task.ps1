# Registers the daily lead-retention purge.
#
# Runs purge_old_leads.py --apply once a day: rejected leads are deleted 30
# days after rejection, every other finished lead a year after it finished, and
# leads still awaiting review are never touched. See retention.py for the
# policy and README "Retention" for why the status page keeps its numbers.
#
# Deliberately scheduled AFTER the Oracle outcome sync (06:00). The sync is what
# fills in qualification and conversion for a lead, and those numbers are rolled
# up into purged_lead_stats at the moment the lead is deleted -- purging first
# would bank a lead's outcome as whatever had been synced the previous day.
param(
    [string]$At = '07:00'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$purgeScript = Join-Path $projectRoot 'purge_old_leads.py'
$userId = "$env:USERDOMAIN\$env:USERNAME"
$runAt = [datetime]::Today.Add([timespan]::Parse($At))

$action = New-ScheduledTaskAction -Execute $python `
    -Argument ('"{0}" --apply' -f $purgeScript) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $runAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $userId `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'LeadPipeline_Retention' `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Daily deletion of finished leads past the retention policy. Counts are preserved for the status page; the lead rows and their cached email are not.' `
    -Force | Out-Null

Write-Host "[OK] Registered LeadPipeline_Retention daily at $At."
Write-Host "     Dry run any time with: py purge_old_leads.py"
