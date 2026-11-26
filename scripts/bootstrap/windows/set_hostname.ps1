# ==============================================================================
# Change Windows Computer Name (takes effect after reboot)
# ==============================================================================
$ErrorActionPreference = 'Stop'

$newName = $env:NEW_HOSTNAME
if ([string]::IsNullOrWhiteSpace($newName)) {
    Write-Output "[ERROR] NEW_HOSTNAME cannot be empty."
    exit 2
}
$newName = $newName.Trim()

Write-Output "=== Changing Windows Computer Name ==="
$currentName = $env:COMPUTERNAME
$pendingName = (Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName" -Name "ComputerName" -ErrorAction SilentlyContinue).ComputerName

if ($currentName -ieq $newName) {
    Write-Output "[OK] Computer name is already '$currentName'."
    exit 0
}
if ($pendingName -ieq $newName) {
    Write-Output "[OK] Computer rename to '$newName' is already pending reboot (current: '$currentName')."
    exit 0
}

Write-Output "[ACTION] Renaming computer '$currentName' -> '$newName' ..."
Rename-Computer -NewName $newName -Force -ErrorAction Stop
Write-Output "[OK] Computer name scheduled to change to '$newName' (will take effect after reboot)."
exit 0
