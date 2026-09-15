$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = (Get-Command pythonw -ErrorAction Stop).Source
}
$serve = Join-Path $root 'dashboard\serve.py'
$userId = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $serve) `
    -WorkingDirectory $root
$logon = New-ScheduledTaskTrigger -AtLogOn -User $userId
# A lightweight heartbeat recovers a task that was terminated without Windows
# classifying it as a normal process failure. MultipleInstances=IgnoreNew keeps
# this from creating a second dashboard while the first one is healthy.
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $userId `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'LeadPipeline_Dashboard' -Action $action `
    -Trigger @($logon, $heartbeat) -Settings $settings -Principal $principal `
    -Description 'Local Bal Seal lead-review dashboard; restartable and single-instance.' `
    -Force | Out-Null

Write-Host '[OK] Registered resilient LeadPipeline_Dashboard task.'
