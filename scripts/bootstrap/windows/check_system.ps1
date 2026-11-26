# ==============================================================================
# Post-bootstrap system verification: services, disks, network/DNS, hostname
# ==============================================================================
$ErrorActionPreference = 'Continue'

$csSkip = ($env:CS_SKIP -as [string])
$skipCs = ($csSkip -and $csSkip.Trim().ToLower() -in @("true", "1", "yes"))
$expectedHost = if ([string]::IsNullOrWhiteSpace($env:NEW_HOSTNAME)) { "" } else { $env:NEW_HOSTNAME.Trim() }

Write-Output "=== Windows Bootstrap Verification ==="

# 1. Hostname check (current & pending)
$activeHost = $env:COMPUTERNAME
$pendingHost = (Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName" -Name "ComputerName" -ErrorAction SilentlyContinue).ComputerName
Write-Output "[INFO] Hostname: active='$activeHost', pending='$pendingHost' (expected='$expectedHost')"

# 2. Services check
$services = @("CcmExec", "Zabbix Agent")
if (-not $skipCs) {
    $services += "CSFalconService"
}

$failedServices = @()
foreach ($svcName in $services) {
    $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') {
        Write-Output "[OK] Service '$svcName' is Running."
    } elseif ($svc) {
        Write-Output "[FAIL] Service '$svcName' is $($svc.Status)."
        $failedServices += $svcName
    } else {
        Write-Output "[FAIL] Service '$svcName' is not installed."
        $failedServices += $svcName
    }
}

# 3. Scheduled task check
$null = & schtasks.exe /query /tn "Microsoft\HHCloud_Zabbix" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Output "[WARN] Scheduled task 'Microsoft\HHCloud_Zabbix' still exists."
} else {
    Write-Output "[OK] Scheduled task 'Microsoft\HHCloud_Zabbix' is absent."
}

# 4. Disk & Volume summary
Write-Output "--- Disks ---"
Get-Disk | Sort-Object Number |
    Format-Table Number, PartitionStyle, OperationalStatus, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, @{N='UnallocGB';E={[math]::Round(($_.Size - $_.AllocatedSize)/1GB,2)}} -AutoSize |
    Out-String | ForEach-Object { $_.TrimEnd() } | Write-Output

Write-Output "--- Volumes ---"
Get-Volume | Where-Object { $_.DriveLetter } | Sort-Object DriveLetter |
    Format-Table DriveLetter, FileSystem, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, @{N='FreeGB';E={[math]::Round($_.SizeRemaining/1GB,2)}} -AutoSize |
    Out-String | ForEach-Object { $_.TrimEnd() } | Write-Output

# 5. Network & DNS summary
Write-Output "--- Network & DNS ---"
Get-NetIPConfiguration | Where-Object { $_.IPv4Address } | ForEach-Object {
    $ips = ($_.IPv4Address | ForEach-Object { $_.IPAddress }) -join ', '
    $dns = ($_.DNSServer | Where-Object { $_.AddressFamily -eq 2 } | ForEach-Object { $_.ServerAddresses }) -join ', '
    Write-Output "[INFO] $($_.InterfaceAlias): IP=[$ips] DNS=[$dns]"
}

if ($failedServices.Count -gt 0) {
    Write-Output "[ERROR] One or more required services are not running: $($failedServices -join ', ')"
    exit 1
}

Write-Output "[SUCCESS] All verification checks passed."
exit 0
