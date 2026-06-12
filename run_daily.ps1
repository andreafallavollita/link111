# run_daily.ps1
# LinkedIn growth agent: daily orchestrator.
#
# Sequence:
#   1. Init DB (idempotent).
#   2. Import Excel lead list if present (idempotent, skips duplicates).
#   3. Count queue (status discovered/queued).
#   4. If queue < QueueThreshold, run discovery_agent.
#   5. Run sender_v3 with Target/StartHour/EndHour.
#   6. Write run summary to growth_run.log and update last_run.json.
#
# Designed to be invoked by Windows Task Scheduler at 09:00 daily.

[CmdletBinding()]
param(
    [int]$Target = 40,
    [int]$StartHour = 9,
    [int]$EndHour = 20,
    [int]$QueueThreshold = 100,
    [switch]$SkipDiscovery,
    [switch]$SkipSender,
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
$Python = 'C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogFile = Join-Path $ProjectDir 'growth_run.log'
$LastRunFile = Join-Path $ProjectDir 'last_run.json'

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Invoke-PyScript {
    param(
        [string]$Script,
        [string[]]$ScriptArgs = @()
    )
    $scriptPath = Join-Path $ProjectDir $Script
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        Write-Log "Script not found: $scriptPath" 'ERROR'
        return $false
    }
    Write-Log "Launching $Script $($ScriptArgs -join ' ')"
    $allArgs = @($scriptPath) + $ScriptArgs
    & $Python @allArgs 2>&1 | ForEach-Object { Write-Log $_ 'PY' }
    $code = $LASTEXITCODE
    Write-Log "$Script exited with code $code"
    return ($code -eq 0)
}

function Get-QueueSize {
    $tmp = Join-Path $ProjectDir '_queue_check.py'
    @"
import sqlite3
try:
    db_path = r'$ProjectDir\linkedin_growth.db'
    c = sqlite3.connect(db_path)
    r = c.execute("SELECT COUNT(*) FROM prospects WHERE status IN ('discovered','queued')").fetchone()[0]
    print(r)
    c.close()
except Exception:
    print(0)
"@ | Set-Content -LiteralPath $tmp -Encoding UTF8
    $out = & $Python $tmp 2>&1
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    if ($out -match '(\d+)') {
        $val = [int]$Matches[1]
        return $val
    }
    return 0
}

function Write-LastRun {
    param(
        [string]$Component,
        [string]$Status,
        [hashtable]$Extra = @{}
    )
    $payload = @{
        component = $Component
        status = $Status
        timestamp = (Get-Date).ToString('o')
        host = $env:COMPUTERNAME
        extra = $Extra
    }
    $json = $payload | ConvertTo-Json -Depth 4
    Set-Content -LiteralPath $LastRunFile -Value $json -Encoding UTF8
}

Push-Location $ProjectDir
try {
    Write-Log "==================== DAILY RUN START ====================" 'RUN'
    Write-Log "Project: $ProjectDir"
    Write-Log "Python: $Python"
    Write-Log "Params: Target=$Target StartHour=$StartHour EndHour=$EndHour QueueThreshold=$QueueThreshold"

    # Heartbeat: start
    & $Python (Join-Path $ProjectDir 'health_monitor.py') ping --status start 2>&1 | Out-Null

    # A. Init DB
    if (-not (Invoke-PyScript -Script 'growth_db.py')) {
        Write-Log "DB init failed; aborting." 'ERROR'
        Write-LastRun -Component 'orchestrator' -Status 'db_init_failed'
        & $Python (Join-Path $ProjectDir 'health_monitor.py') ping --status fail 2>&1 | Out-Null
        & $Python (Join-Path $ProjectDir 'health_monitor.py') telegram --message "LinkedIn growth: DB init failed on $env:COMPUTERNAME" 2>&1 | Out-Null
        exit 2
    }

    # B. Import Excel lead list if present
    $ExcelFile = Join-Path $ProjectDir 'lead_b2b_eu_martech_growth_500_best_effort_2026-06-11.xlsx'
    if (Test-Path -LiteralPath $ExcelFile) {
        Write-Log "Excel lead list found, importing prospects..."
        Invoke-PyScript -Script 'import_excel_prospects.py' -ScriptArgs @($ExcelFile)
    } else {
        Write-Log "No Excel lead list found, skipping import." 'WARN'
    }

    # C. Count queue
    $queue = Get-QueueSize
    Write-Log "Queue size: $queue (discovered + queued)"

    # D. Discovery only if queue is below threshold
    if (-not $SkipDiscovery) {
        if ($queue -lt $QueueThreshold) {
            $needed = $QueueThreshold - $queue + 10
            Write-Log "Queue below threshold ($QueueThreshold), running discovery (need ~$needed more)..."
            $discArgs = @(
                '--mode',        'engagement',
                '--max-queries', '10',
                '--max-hashtags','10',
                '--min-score',   '30',
                '--queue-target',"$needed"
            )
            Invoke-PyScript -Script 'discovery_agent.py' -ScriptArgs $discArgs
            $queue = Get-QueueSize
            Write-Log "Queue after discovery: $queue"
        } else {
            Write-Log "Queue is sufficient ($queue >= $QueueThreshold), skipping discovery."
        }
    } else {
        Write-Log "Discovery skipped by flag."
    }

    # E. Sender
    if (-not $SkipSender) {
        $senderArgs = @(
            '--target', $Target,
            '--start-hour', $StartHour,
            '--end-hour', $EndHour
        )
        if ($DryRun) { $senderArgs += '--dry-run' }
        Write-Log "Running sender (target=$Target, hours=$StartHour-$EndHour)..."
        Invoke-PyScript -Script 'sender_v3.py' -ScriptArgs $senderArgs | Out-Null
    } else {
        Write-Log "Sender skipped by flag."
    }

    # F. Final report
    Write-Log "Generating dashboard..."
    Invoke-PyScript -Script 'report_growth.py' -ScriptArgs @('--csv') | Out-Null

    Write-LastRun -Component 'orchestrator' -Status 'ok' -Extra @{
        target = $Target
        hours = "$StartHour-$EndHour"
    }

    # Telegram summary (best-effort)
    $summary = "LinkedIn growth daily run completed on $env:COMPUTERNAME at $(Get-Date -Format 'HH:mm'). Target=$Target hours=$StartHour-$EndHour. Queue=$queue."
    & $Python (Join-Path $ProjectDir 'health_monitor.py') telegram --message $summary 2>&1 | Out-Null
    & $Python (Join-Path $ProjectDir 'health_monitor.py') ping --status success 2>&1 | Out-Null

    Write-Log "==================== DAILY RUN DONE =====================" 'RUN'
    exit 0
}
catch {
    Write-Log "FATAL: $($_.Exception.Message)" 'ERROR'
    Write-Log $_.ScriptStackTrace 'ERROR'
    Write-LastRun -Component 'orchestrator' -Status 'error' -Extra @{
        error = $_.Exception.Message
    }
    & $Python (Join-Path $ProjectDir 'health_monitor.py') ping --status fail 2>&1 | Out-Null
    & $Python (Join-Path $ProjectDir 'health_monitor.py') telegram --message "LinkedIn growth: FATAL error on $env:COMPUTERNAME : $($_.Exception.Message)" 2>&1 | Out-Null
    exit 1
}
finally {
    Pop-Location
}
