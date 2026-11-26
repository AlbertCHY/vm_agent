# ==============================================================================
# Rescan disks and expand C: partition (safely removing an adjacent trailing
# Recovery partition only when unallocated space exists behind it)
# ==============================================================================
$ErrorActionPreference = 'Stop'

$minFreeBytes = 1GB

Write-Output "=== Windows C: Drive Rescan & Expansion Start ==="

try {
    Update-HostStorageCache
} catch {
    Write-Output "[WARN] Update-HostStorageCache returned warning: $($_.Exception.Message)"
}
Start-Sleep -Seconds 2

$cPart = Get-Partition -DriveLetter C -ErrorAction Stop
$diskNum = $cPart.DiskNumber
$disk = Get-Disk -Number $diskNum -ErrorAction Stop
$beforeSize = $cPart.Size
$unallocated = [int64]$disk.Size - [int64]$disk.AllocatedSize

Write-Output ("[INFO] C: Partition : Disk {0} Partition {1} (Current: {2:N2} GB)" -f $diskNum, $cPart.PartitionNumber, ($beforeSize / 1GB))
Write-Output ("[INFO] Parent Disk  : Disk {0} (Total: {1:N2} GB, Unallocated: {2:N2} GB)" -f $diskNum, ($disk.Size / 1GB), ($unallocated / 1GB))

if ($unallocated -lt $minFreeBytes) {
    Write-Output "[OK] No significant unallocated space (>= 1 GB) on Disk $diskNum. C: expansion not needed."
    Get-Volume -DriveLetter C | Format-Table DriveLetter, FileSystem, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, @{N='FreeGB';E={[math]::Round($_.SizeRemaining/1GB,2)}} -AutoSize | Out-String | Write-Output
    exit 0
}

# Check if a Recovery partition immediately follows C: and blocks expansion
$allParts = @(Get-Partition -DiskNumber $diskNum | Sort-Object Offset)
$cIdx = -1
for ($i = 0; $i -lt $allParts.Count; $i++) {
    if ($allParts[$i].PartitionNumber -eq $cPart.PartitionNumber) {
        $cIdx = $i
        break
    }
}

$recoveryGptGuid = "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}"
if ($cIdx -ge 0 -and ($cIdx + 1) -lt $allParts.Count) {
    $nextPart = $allParts[$cIdx + 1]
    $isRecovery = ($nextPart.Type -eq 'Recovery') -or ($nextPart.GptType -ieq $recoveryGptGuid)
    if ($isRecovery) {
        Write-Output ("[ACTION] Detected blocking Recovery partition (Disk {0} Partition {1}, {2:N0} MB) immediately after C:. Removing..." -f $diskNum, $nextPart.PartitionNumber, ($nextPart.Size / 1MB))
        try {
            Remove-Partition -DiskNumber $diskNum -PartitionNumber $nextPart.PartitionNumber -Confirm:$false -ErrorAction Stop
            Write-Output "[OK] Removed blocking Recovery partition via Remove-Partition."
        } catch {
            Write-Output "[INFO] Remove-Partition blocked ($($_.Exception.Message)); falling back to diskpart delete partition override ..."
            $dpFile = Join-Path $env:TEMP ("expand_c_" + [guid]::NewGuid().ToString("N") + ".txt")
            try {
                @(
                    "select disk $diskNum",
                    "select partition $($nextPart.PartitionNumber)",
                    "delete partition override"
                ) | Set-Content -LiteralPath $dpFile -Encoding ASCII
                $dpOut = & diskpart.exe /s $dpFile 2>&1
                if ($LASTEXITCODE -ne 0) {
                    Write-Output "[ERROR] diskpart failed removing Recovery partition: $($dpOut -join ' ')"
                    exit 10
                }
                Write-Output "[OK] Removed blocking Recovery partition via diskpart."
            } finally {
                Remove-Item -LiteralPath $dpFile -Force -ErrorAction SilentlyContinue
            }
        }
        try { Update-HostStorageCache } catch {}
        Start-Sleep -Seconds 2
    } else {
        Write-Output ("[WARN] Partition {0} (Type={1}) sits immediately after C: and is NOT a Recovery partition. Skipping deletion for safety." -f $nextPart.PartitionNumber, $nextPart.Type)
    }
}

$cPart = Get-Partition -DriveLetter C
$supported = Get-PartitionSupportedSize -DriveLetter C
$growBytes = [int64]$supported.SizeMax - [int64]$cPart.Size

if ($growBytes -le 100MB) {
    Write-Output "[ERROR] Unallocated space detected on Disk $diskNum, but C: cannot expand further (max=$($supported.SizeMax), current=$($cPart.Size))."
    exit 11
}

Write-Output ("[ACTION] Expanding C: from {0:N2} GB to {1:N2} GB ..." -f ($cPart.Size / 1GB), ($supported.SizeMax / 1GB))
Resize-Partition -DriveLetter C -Size $supported.SizeMax

$afterPart = Get-Partition -DriveLetter C
Write-Output ("[SUCCESS] C: expanded: {0:N2} GB -> {1:N2} GB" -f ($beforeSize / 1GB), ($afterPart.Size / 1GB))
Get-Volume -DriveLetter C | Format-Table DriveLetter, FileSystem, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, @{N='FreeGB';E={[math]::Round($_.SizeRemaining/1GB,2)}} -AutoSize | Out-String | Write-Output
exit 0
