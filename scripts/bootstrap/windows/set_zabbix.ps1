# ==============================================================================
# Update Windows Zabbix Agent configuration and restart service
# ==============================================================================
$ErrorActionPreference = 'Stop'

Write-Output "=== Windows Zabbix Agent Configuration Update ==="

$zabbixServers = $env:ZABBIX_SERVERS
$zabbixConf = if ([string]::IsNullOrWhiteSpace($env:ZABBIX_CONF)) {
    "C:\Program Files\Zabbix Agent\zabbix_agentd.conf"
} else {
    $env:ZABBIX_CONF.Trim()
}
$serviceName = if ([string]::IsNullOrWhiteSpace($env:ZABBIX_SERVICE)) {
    "Zabbix Agent"
} else {
    $env:ZABBIX_SERVICE.Trim()
}

if ([string]::IsNullOrWhiteSpace($zabbixServers)) {
    Write-Output "[ERROR] ZABBIX_SERVERS is not set (please configure ZABBIX_SERVERS in .env)."
    exit 2
}

$localIp = $env:TARGET_IP
if ([string]::IsNullOrWhiteSpace($localIp)) {
    $ipObj = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1
    $localIp = if ($ipObj) { $ipObj.IPAddress } else { $env:COMPUTERNAME }
}
$localIp = $localIp.Trim()

if (-not (Test-Path -LiteralPath $zabbixConf)) {
    Write-Output "[WARN] Cannot find Zabbix configuration file at $zabbixConf, skipping."
    exit 0
}

$lines = Get-Content -LiteralPath $zabbixConf -Encoding UTF8
$hasServer = $false
$hasServerActive = $false
$hasHostname = $false

$updated = foreach ($line in $lines) {
    if ($line -match '^Server=') {
        $hasServer = $true
        "Server=$zabbixServers"
    } elseif ($line -match '^ServerActive=') {
        $hasServerActive = $true
        "ServerActive=$zabbixServers"
    } elseif ($line -match '^Hostname=') {
        $hasHostname = $true
        "Hostname=$localIp"
    } else {
        $line
    }
}

$resultLines = [System.Collections.Generic.List[string]]::new()
foreach ($l in $updated) { $resultLines.Add([string]$l) }
if (-not $hasServer)       { $resultLines.Add("Server=$zabbixServers") }
if (-not $hasServerActive) { $resultLines.Add("ServerActive=$zabbixServers") }
if (-not $hasHostname)     { $resultLines.Add("Hostname=$localIp") }

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($zabbixConf, $resultLines, $utf8NoBom)
Write-Output "[OK] Updated $zabbixConf (Hostname=$localIp)"

$svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Output "[ERROR] Windows service '$serviceName' not found."
    exit 3
}

Write-Output "[ACTION] Restarting service '$serviceName' ..."
Restart-Service -Name $serviceName -Force
Start-Sleep -Seconds 3

$svc = Get-Service -Name $serviceName
if ($svc.Status -eq 'Running') {
    Write-Output "[OK] Zabbix config updated (Hostname=$localIp) and '$serviceName' is Running."
    exit 0
} else {
    Write-Output "[ERROR] Service '$serviceName' failed to start (Status=$($svc.Status))."
    exit 3
}
