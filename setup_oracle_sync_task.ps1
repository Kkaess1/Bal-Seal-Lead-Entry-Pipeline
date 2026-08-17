param(
    [string]$At = '06:00'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$syncScript = Join-Path $projectRoot 'oracle_sync.py'
$userId = "$env:USERDOMAIN\$env:USERNAME"
$runAt = [datetime]::Today.Add([timespan]::Parse($At))

$action = New-ScheduledTaskAction -Execute $python `
    -Argument ('"{0}"' -f $syncScript) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $runAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $userId `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'LeadPipeline_OracleOutcomeSync' `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Daily read-only Oracle Sales lead qualification, conversion, and opportunity value sync.' `
    -Force | Out-Null

Write-Host "[OK] Registered LeadPipeline_OracleOutcomeSync daily at $At."
