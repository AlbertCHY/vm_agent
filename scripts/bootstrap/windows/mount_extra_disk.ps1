# ==============================================================================
# Scan unconfigured extra disks on Windows:
# Online -> Initialize GPT -> Single full partition -> Format NTFS -> Assign
# drive letter starting from START_DRIVE_LETTER (default: E), skipping occupied
# ==============================================================================
$ErrorActionPreference = 'Stop'

Write-Output "=== Windows Extra Data Disk Setup Start ==="

$startLetter = if ([string]::IsNullOrWhiteSpace($env:START_DRIVE_LETTER)) {
    'E'
} else {
    $env:START_DRIVE_LETTER.Trim().ToUpper()[0]
}
if ($startLetter -lt 'C' -or $startLetter -gt 'Z') {
    Write-Output "[ERROR] Invalid START_DRIVE_LETTER: '$startLetter'"
    exit 2
}

try {
    Update-HostStorageCache
} catch {
    Write-Output "[WARN] Update-HostStorageCache returned warning: $($_.Exception.Message)"
}
Start-Sleep -Seconds 2

$candidateDisks = @(Get-Disk | Where-Object {
    (-not $_.IsBoot) -and (-not $_.IsSystem) -and (
        ($_.PartitionStyle -eq 'RAW') -or ($_.NumberOfPartitions -eq 0)
    )
} | Sort-Object Number)

if ($candidateDisks.Count -eq 0) {
    Write-Output "[OK] No additional unconfigured disk found. Nothing to initialize."
    exit 0
}

function Get-NextFreeDriveLetter {
    param([char]$FromLetter)

    $used = [System.Collections.Generic.HashSet[char]]::new()
    foreach ($v in (Get-Volume -ErrorAction SilentlyContinue)) {
        if ($v.DriveLetter) { [void]$used.Add([char]::ToUpper($v.DriveLetter)) }
    }
    foreach ($p in (Get-Partition -ErrorAction SilentlyContinue)) {
        if ($p.DriveLetter) { [void]$used.Add([char]::ToUpper($p.DriveLetter)) }
    }
    foreach ($ld in (Get-CimInstance -ClassName Win32_LogicalDisk -ErrorAction SilentlyContinue)) {
        if ($ld.DeviceID -and $ld.DeviceID.Length -ge 1) {
            [void]$used.Add([char]::ToUpper($ld.DeviceID[0]))
        }
    }

    for ($code = [int]$FromLetter; $code -le [int][char]'Z'; $code++) {
        $ch = [char]$code
        if (-not $used.Contains($ch)) {
            return $ch
        }
    }
    return $null
}

foreach ($d in $candidateDisks) {
    $num = $d.Number
    Write-Output ("[ACTION] Processing Disk {0} (Size={1:N2} GB, Style={2}, Offline={3}) ..." -f $num, ($d.Size / 1GB), $d.PartitionStyle, $d.IsOffline)

    if ($d.IsOffline) {
        Set-Disk -Number $num -IsOffline $false
        Write-Output "[OK] Disk $num brought Online."
    }
    if ($d.IsReadOnly) {
        Set-Disk -Number $num -IsReadOnly $false
        Write-Output "[OK] Disk $num set to ReadWrite."
    }

    $d = Get-Disk -Number $num
    if ($d.PartitionStyle -eq 'RAW') {
        Initialize-Disk -Number $num -PartitionStyle GPT
        Write-Output "[OK] Disk $num initialized as GPT."
    }

    $letter = Get-NextFreeDriveLetter -FromLetter $startLetter
    if ($null -eq $letter) {
        Write-Output "[ERROR] No available drive letters remaining from ${startLetter}: to Z:."
        exit 3
    }

    Write-Output "[ACTION] Creating single maximum partition on Disk $num with drive letter ${letter}: ..."
    New-Partition -DiskNumber $num -UseMaximumSize -DriveLetter $letter | Out-Null

    Write-Output "[ACTION] Formatting ${letter}: as NTFS (no custom label) ..."
    Format-Volume -DriveLetter $letter -FileSystem NTFS -Confirm:$false | Out-Null

    Write-Output "[SUCCESS] Disk $num mounted and formatted at ${letter}:."
}

Write-Output "=== Extra Disk Setup Completed ==="
Get-Volume | Where-Object { $_.DriveLetter } |
    Sort-Object DriveLetter |
    Format-Table DriveLetter, FileSystem, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, @{N='FreeGB';E={[math]::Round($_.SizeRemaining/1GB,2)}} -AutoSize |
    Out-String | Write-Output
exit 0
