# ==============================================================================
# Deploy SSH public key to IT user's ~/.ssh/authorized_keys on Windows
# and idempotently comment out the default Match Group administrators block
# in C:\ProgramData\ssh\sshd_config (restarting sshd safely via detached process
# only when sshd_config actually changes).
# ==============================================================================
$ErrorActionPreference = 'Stop'

$pubKey = $env:SSH_PUB_KEY
if ([string]::IsNullOrWhiteSpace($pubKey)) {
    Write-Output "[ERROR] SSH_PUB_KEY is empty."
    exit 1
}
$pubKey = $pubKey.Trim()

$targetUser = if ([string]::IsNullOrWhiteSpace($env:TARGET_USER)) {
    $env:USERNAME
} else {
    $env:TARGET_USER.Trim()
}

if ($targetUser -ieq $env:USERNAME -and -not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $userHome = $env:USERPROFILE
} else {
    $userHome = Join-Path "C:\Users" $targetUser
}

if (-not (Test-Path -LiteralPath $userHome)) {
    Write-Output "[ERROR] User profile directory not found: $userHome"
    exit 2
}

$sshDir = Join-Path $userHome ".ssh"
$keyFile = Join-Path $sshDir "authorized_keys"

Write-Output "[INFO] Checking $sshDir for user '$targetUser' ..."
if (-not (Test-Path -LiteralPath $sshDir)) {
    New-Item -ItemType Directory -Path $sshDir -Force | Out-Null
}

# Extract base64 key body (2nd token) for idempotent deduplication
$tokens = $pubKey -split '\s+'
$keyBody = if ($tokens.Count -ge 2) { $tokens[1] } else { $pubKey }

$existing = ""
if (Test-Path -LiteralPath $keyFile) {
    $existing = [System.IO.File]::ReadAllText($keyFile)
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
if ($existing -and $existing.Contains($keyBody)) {
    Write-Output "[OK] Pubkey already exists in $keyFile"
} else {
    $prefix = if ($existing -and -not $existing.EndsWith("`n")) { "`r`n" } else { "" }
    [System.IO.File]::WriteAllText($keyFile, $existing + $prefix + $pubKey + "`r`n", $utf8NoBom)
    Write-Output "[SUCCESS] Pubkey inserted into $keyFile"
}

# Idempotently comment out default 'Match Group administrators' + administrators_authorized_keys
$sshdConfig = "C:\ProgramData\ssh\sshd_config"
if (-not (Test-Path -LiteralPath $sshdConfig)) {
    Write-Output "[WARN] sshd_config not found at $sshdConfig; skipping sshd_config update."
    exit 0
}

$rawLines = [System.IO.File]::ReadAllLines($sshdConfig)
$outLines = [System.Collections.Generic.List[string]]::new()
$modified = $false
$inAdminMatch = $false

foreach ($line in $rawLines) {
    if ($line -match '^\s*Match\s+Group\s+administrators\s*$') {
        $outLines.Add("# $line")
        $modified = $true
        $inAdminMatch = $true
        continue
    }
    if ($inAdminMatch) {
        if ($line -match '^\s*$') {
            $outLines.Add($line)
            continue
        }
        if ($line -match '^\s*AuthorizedKeysFile\s+.*administrators_authorized_keys\s*$') {
            $outLines.Add("# $line")
            $modified = $true
            $inAdminMatch = $false
            continue
        }
        $inAdminMatch = $false
    }
    $outLines.Add($line)
}

if (-not $modified) {
    Write-Output "[OK] sshd_config already allows per-user authorized_keys; no restart needed."
    exit 0
}

$backupPath = "$sshdConfig.bak"
Copy-Item -LiteralPath $sshdConfig -Destination $backupPath -Force
[System.IO.File]::WriteAllLines($sshdConfig, $outLines, $utf8NoBom)
Write-Output "[ACTION] Commented out Match Group administrators block in $sshdConfig."

# Validate sshd_config syntax before restarting sshd
$sshdExe = $null
$cmdObj = Get-Command sshd.exe -ErrorAction SilentlyContinue
if ($cmdObj) {
    $sshdExe = $cmdObj.Source
} else {
    foreach ($cand in @("C:\Program Files\OpenSSH\sshd.exe", "C:\Windows\System32\OpenSSH\sshd.exe")) {
        if (Test-Path -LiteralPath $cand) {
            $sshdExe = $cand
            break
        }
    }
}

if ($sshdExe) {
    $testOut = & $sshdExe -t 2>&1
    if ($LASTEXITCODE -ne 0) {
        Copy-Item -LiteralPath $backupPath -Destination $sshdConfig -Force
        Write-Output "[ERROR] sshd -t syntax validation failed; restored backup. Output: $($testOut -join ' ')"
        exit 3
    }
    Write-Output "[OK] sshd_config syntax validated via '$sshdExe -t'."
}

# Schedule detached sshd restart outside sshd's process tree so this SSH command exits cleanly first
$restartCmd = 'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Restart-Service sshd -Force"'
try {
    $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $restartCmd }
    if ($res.ReturnValue -ne 0) {
        throw "Win32_Process.Create returned $($res.ReturnValue)"
    }
    Write-Output "[OK] Detached sshd restart scheduled (PID=$($res.ProcessId)). [SSHD_RESTART_SCHEDULED]"
} catch {
    Write-Output "[WARN] Win32_Process.Create failed ($($_.Exception.Message)); falling back to Start-Process ..."
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", "Start-Sleep -Seconds 2; Restart-Service sshd -Force")
    Write-Output "[OK] Detached sshd restart scheduled. [SSHD_RESTART_SCHEDULED]"
}

exit 0
