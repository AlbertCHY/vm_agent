# ==============================================================================
# Install and Configure CrowdStrike Falcon Sensor on Windows (Non-interactive)
# CID and Provisioning Token are provided via secret_env
# ==============================================================================
$ErrorActionPreference = 'Stop'

$serviceName = "CSFalconService"
$csType = if ([string]::IsNullOrWhiteSpace($env:CS_INSTALL_TYPE)) { "NGAV" } else { $env:CS_INSTALL_TYPE.Trim().ToUpper() }
$csDir = if ([string]::IsNullOrWhiteSpace($env:CS_DIR)) {
    Join-Path $PSScriptRoot "CrowdStrike"
} else {
    $env:CS_DIR.Trim()
}
$proxyHost = $env:CS_PROXY_HOST
$proxyPort = $env:CS_PROXY_PORT

Write-Output "=== Starting CrowdStrike Installation Workflow (Mode: $csType) ==="

$existingSvc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($existingSvc -and $existingSvc.Status -eq 'Running') {
    Write-Output "[OK] CrowdStrike ($serviceName) is already running, skipping installation."
    exit 0
}

$installerItem = Get-ChildItem -Path $csDir -Filter "FalconSensor_Windows*.exe" -File -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $installerItem) {
    Write-Output "[ERROR] CrowdStrike installer (FalconSensor_Windows*.exe) not found in $csDir"
    exit 3
}

if ($csType -in @("M", "MDR")) {
    $cid = $env:MDR_CID
    $token = $env:MDR_TOKEN
    Write-Output "[INFO] Selected mode: MDR"
} else {
    $cid = $env:NGAV_CID
    $token = $env:NGAV_TOKEN
    Write-Output "[INFO] Selected mode: NGAV"
}

if ([string]::IsNullOrWhiteSpace($cid)) {
    Write-Output "[ERROR] CrowdStrike CID for mode '$csType' is empty (check .env)."
    exit 2
}

$argList = [System.Collections.Generic.List[string]]::new()
$argList.Add("/install")
$argList.Add("/quiet")
$argList.Add("/norestart")
$argList.Add("CID=$($cid.Trim())")
if (-not [string]::IsNullOrWhiteSpace($proxyHost)) {
    $argList.Add("APP_PROXYNAME=$($proxyHost.Trim())")
}
if (-not [string]::IsNullOrWhiteSpace($proxyPort)) {
    $argList.Add("APP_PROXYPORT=$($proxyPort.Trim())")
}
$argList.Add("ProvWaitTime=1200000")
if (-not [string]::IsNullOrWhiteSpace($token)) {
    $argList.Add("ProvToken=$($token.Trim())")
}

Write-Output "[ACTION] Installing $($installerItem.Name) (silent, norestart) ..."
$proc = Start-Process -FilePath $installerItem.FullName -ArgumentList $argList -Wait -PassThru
$rc = $proc.ExitCode
Write-Output "[INFO] Installer exited with code: $rc"

# 0 = success, 3010 = success (reboot required)
if ($rc -ne 0 -and $rc -ne 3010) {
    Write-Output "[ERROR] CrowdStrike installation failed with exit code $rc."
    exit 4
}

# Wait up to 30 seconds for CSFalconService to come up
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    $svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') {
        Write-Output "[OK] $serviceName is Running."
        exit 0
    }
    Start-Sleep -Seconds 3
}

$svc = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($svc) {
    Write-Output "[ERROR] $serviceName exists but is not Running (Status=$($svc.Status))."
} else {
    Write-Output "[ERROR] $serviceName was not found after installation."
}
exit 6
