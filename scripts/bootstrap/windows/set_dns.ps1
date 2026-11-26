# ==============================================================================
# Configure static DNS servers on the active Windows network adapter
# ==============================================================================
$ErrorActionPreference = 'Stop'

$rawDns = $env:WINDOWS_DNS_SERVERS
$targetIp = ($env:TARGET_IP)

if ([string]::IsNullOrWhiteSpace($rawDns)) {
    Write-Output "[INFO] WINDOWS_DNS_SERVERS is empty, skipping DNS configuration."
    exit 0
}

$dnsList = @($rawDns -split '[,;\s]+' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
if ($dnsList.Count -eq 0) {
    Write-Output "[INFO] No valid DNS entries parsed from WINDOWS_DNS_SERVERS, skipping."
    exit 0
}

Write-Output "=== Configuring Windows DNS Servers ==="
$ifIndex = $null
$ifAlias = ""

if (-not [string]::IsNullOrWhiteSpace($targetIp)) {
    $ipObj = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $targetIp.Trim() } |
        Select-Object -First 1
    if ($ipObj) {
        $ifIndex = $ipObj.InterfaceIndex
        $ifAlias = $ipObj.InterfaceAlias
    }
}

if ($null -eq $ifIndex) {
    $adapter = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Up' } |
        Where-Object {
            $ips = Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' }
            $null -ne $ips
        } |
        Select-Object -First 1

    if ($adapter) {
        $ifIndex = $adapter.ifIndex
        $ifAlias = $adapter.Name
    }
}

if ($null -eq $ifIndex) {
    Write-Output "[ERROR] Unable to detect active network adapter for DNS configuration."
    exit 2
}

Write-Output "[INFO] Target adapter: '$ifAlias' (InterfaceIndex=$ifIndex)"
Write-Output "[ACTION] Setting DNS servers to: $($dnsList -join ', ')"
Set-DnsClientServerAddress -InterfaceIndex $ifIndex -ServerAddresses $dnsList

$currentDns = (Get-DnsClientServerAddress -InterfaceIndex $ifIndex -AddressFamily IPv4).ServerAddresses
Write-Output "[OK] DNS updated on '$ifAlias': $($currentDns -join ', ')"
exit 0
