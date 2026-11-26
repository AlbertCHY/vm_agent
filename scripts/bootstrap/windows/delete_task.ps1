# ==============================================================================
# Remove template scheduled task (default: Microsoft\HHCloud_Zabbix)
# ==============================================================================
$ErrorActionPreference = 'Continue'

$targetTask = if ([string]::IsNullOrWhiteSpace($env:TARGET_TASK)) {
    "Microsoft\HHCloud_Zabbix"
} else {
    $env:TARGET_TASK.Trim()
}

Write-Output "=== Removing Scheduled Task ($targetTask) ==="

$null = & schtasks.exe /query /tn $targetTask 2>&1
if ($LASTEXITCODE -eq 0) {
    $delOut = & schtasks.exe /delete /tn $targetTask /f 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Output "[OK] Scheduled task '$targetTask' deleted."
        exit 0
    } else {
        Write-Output "[ERROR] Failed to delete scheduled task '$targetTask': $($delOut -join ' ')"
        exit 2
    }
} else {
    Write-Output "[OK] Scheduled task '$targetTask' not found, skipping."
    exit 0
}
