# ==============================================================================
# Install SCCM Client (CCMSetup) and wait until CcmExec service is Running
# ==============================================================================
$ErrorActionPreference = 'Stop'

Write-Output "=== SCCM Client Installation Start ==="

$siteCode = $env:SCCM_SITE_CODE
$fsp = $env:SCCM_FSP
$smsmp = $env:SCCM_SMSMP
$sccmDir = if ([string]::IsNullOrWhiteSpace($env:SCCM_DIR)) {
    Join-Path $PSScriptRoot "SCCM"
} else {
    $env:SCCM_DIR.Trim()
}
$waitSec = 600
if (-not [string]::IsNullOrWhiteSpace($env:SCCM_WAIT_SECONDS)) {
    $parsed = 0
    if ([int]::TryParse($env:SCCM_WAIT_SECONDS, [ref]$parsed) -and $parsed -gt 0) {
        $waitSec = $parsed
    }
}

$existingSvc = Get-Service -Name "CcmExec" -ErrorAction SilentlyContinue
if ($existingSvc -and $existingSvc.Status -eq 'Running') {
    Write-Output "[OK] SCCM Client (CcmExec) is already installed and Running, skipping installation."
    exit 0
}

if ([string]::IsNullOrWhiteSpace($siteCode) -or [string]::IsNullOrWhiteSpace($fsp) -or [string]::IsNullOrWhiteSpace($smsmp)) {
    Write-Output "[ERROR] SCCM_SITE_CODE, SCCM_FSP, and SCCM_SMSMP must be configured in .env."
    exit 2
}

$installer = Join-Path $sccmDir "ccmsetup.exe"
if (-not (Test-Path -LiteralPath $installer)) {
    Write-Output "[ERROR] SCCM installer not found at $installer"
    exit 3
}

$argList = @(
    "SMSSITECODE=$($siteCode.Trim())",
    "FSP=$($fsp.Trim())",
    "SMSMP=$($smsmp.Trim())"
)

Write-Output "[ACTION] Launching $installer $($argList -join ' ') ..."
$proc = Start-Process -FilePath $installer -ArgumentList $argList -WorkingDirectory $sccmDir -PassThru
Start-Sleep -Seconds 5
if ($proc.HasExited -and $proc.ExitCode -ne 0) {
    Write-Output "[ERROR] ccmsetup.exe exited immediately with code $($proc.ExitCode)."
    exit 4
}

Write-Output "[INFO] Waiting up to ${waitSec}s for CcmExec service to start ..."
$deadline = (Get-Date).AddSeconds($waitSec)
$noSetupSince = $null

while ((Get-Date) -lt $deadline) {
    $svc = Get-Service -Name "CcmExec" -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -eq 'Running') {
            Write-Output "[OK] SCCM Client (CcmExec) service is Running."
            exit 0
        }
        $setupRunning = Get-Process -Name "ccmsetup" -ErrorAction SilentlyContinue
        if (-not $setupRunning -and $svc.Status -eq 'Stopped') {
            Write-Output "[INFO] ccmsetup finished and CcmExec is Stopped; attempting Start-Service ..."
            try {
                Start-Service -Name "CcmExec" -ErrorAction Stop
                Start-Sleep -Seconds 3
                $svc = Get-Service -Name "CcmExec"
                if ($svc.Status -eq 'Running') {
                    Write-Output "[OK] SCCM Client (CcmExec) service started and is Running."
                    exit 0
                }
            } catch {
                Write-Output "[WARN] Start-Service CcmExec failed: $($_.Exception.Message)"
            }
        }
    } else {
        $setupRunning = Get-Process -Name "ccmsetup" -ErrorAction SilentlyContinue
        if (-not $setupRunning) {
            if ($null -eq $noSetupSince) {
                $noSetupSince = Get-Date
            } elseif (((Get-Date) - $noSetupSince).TotalSeconds -ge 60) {
                Write-Output "[ERROR] ccmsetup process ended but CcmExec service was not installed."
                $logPath = "C:\Windows\ccmsetup\Logs\ccmsetup.log"
                if (Test-Path -LiteralPath $logPath) {
                    Write-Output "--- Tail of ccmsetup.log ---"
                    Get-Content -LiteralPath $logPath -Tail 15 | ForEach-Object { Write-Output $_ }
                }
                exit 5
            }
        } else {
            $noSetupSince = $null
        }
    }
    Start-Sleep -Seconds 10
}

Write-Output "[ERROR] Timed out after ${waitSec}s waiting for CcmExec service to start."
$logPath = "C:\Windows\ccmsetup\Logs\ccmsetup.log"
if (Test-Path -LiteralPath $logPath) {
    Write-Output "--- Tail of ccmsetup.log ---"
    Get-Content -LiteralPath $logPath -Tail 15 | ForEach-Object { Write-Output $_ }
}
exit 6
