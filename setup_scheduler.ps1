# setup_scheduler.ps1
# Registers a Windows scheduled task to run the LinkedIn growth agent every
# weekday at 09:00. Also configures the task to wake the PC from sleep (the
# user must enable "Allow wake timers" in Power Options for this to fully work).
#
# Run as Administrator the first time.
#
# Usage:
#   .\setup_scheduler.ps1               # install/update the task
#   .\setup_scheduler.ps1 -Remove       # uninstall the task
#   .\setup_scheduler.ps1 -Run          # run the task immediately for test

[CmdletBinding()]
param(
    [string]$TaskName = 'LinkedInGrowthAgent_Daily',
    [string]$HealthTaskName = 'LinkedInGrowthAgent_Healthcheck',
    [string]$Time = '09:00',
    [int]$HealthCheckIntervalHours = 6,
    [int]$StaleThresholdHours = 36,
    [int]$Target = 40,
    [int]$StartHour = 9,
    [int]$EndHour = 20,
    [switch]$Remove,
    [switch]$Run
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ProjectDir 'run_daily.ps1'

if (-not (Test-Path -LiteralPath $RunScript)) {
    throw "run_daily.ps1 not found at $RunScript"
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Warning "This script should be run as Administrator to fully configure Task Scheduler."
    Write-Warning "Continuing anyway, but some options (wake from sleep) may not stick."
}

if ($Remove) {
    foreach ($name in @($TaskName, $HealthTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Task '$name' removed."
        } else {
            Write-Host "Task '$name' was not registered."
        }
    }
    return
}

if ($Run) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Triggered '$TaskName' now."
    } else {
        Write-Warning "Task '$TaskName' not registered. Install it first (run without -Run)."
    }
    return
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`" -Target $Target -StartHour $StartHour -EndHour $EndHour" `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# Removing DaysOfWeek assignment — it's a read-only property in some PS versions.
# The sender itself decides whether to run on weekends, so a daily trigger is fine.

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'LinkedIn growth agent: discovers prospects and sends connection requests during business hours.'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
Write-Host "Task '$TaskName' registered. It will run daily at $Time."

# ============================================================
# Health check task: runs every N hours, alerts if stale
# ============================================================
$Python = 'C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe'
$HealthScript = Join-Path $ProjectDir 'health_monitor.py'

$healthAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$HealthScript`" listen" `
    -WorkingDirectory $ProjectDir

$healthTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(15) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 365)

$healthSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

$healthTask = New-ScheduledTask `
    -Action $healthAction `
    -Trigger $healthTrigger `
    -Settings $healthSettings `
    -Principal $principal `
    -Description 'LinkedIn growth agent: listens for Telegram commands (/status, /queue, /report) and checks stale runs.'

if (Get-ScheduledTask -TaskName $HealthTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $HealthTaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $HealthTaskName -InputObject $healthTask | Out-Null
Write-Host "Task '$HealthTaskName' registered. It will check Telegram for commands every 15 minutes."
Write-Host "Stale detection runs inside the daily run; healthchecks.io ping handles cloud alert."
Write-Host ""
Write-Host "To verify:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host "To enable wake from sleep, also run as Admin:"
Write-Host "  powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1"
Write-Host "  powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1"
Write-Host "  powercfg /S SCHEME_CURRENT"
Write-Host ""
Write-Host "To remove:  .\setup_scheduler.ps1 -Remove"
Write-Host "To test:    .\setup_scheduler.ps1 -Run"
