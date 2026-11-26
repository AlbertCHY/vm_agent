# ==============================================================================
# Rotate passwords for fixed Windows local accounts (USER1, USER2)
# Passwords are provided via secret_env (never on the command line)
# ==============================================================================
$ErrorActionPreference = 'Stop'

$user1 = $env:USER1
$user2 = $env:USER2
$pwUser1 = $env:PW_USER1
$pwUser2 = $env:PW_USER2

Write-Output "=== Changing Windows Local Account Passwords ==="

if ([string]::IsNullOrWhiteSpace($user1) -or [string]::IsNullOrWhiteSpace($user2)) {
    Write-Output "[ERROR] USER1 and USER2 cannot be empty."
    exit 2
}
if ([string]::IsNullOrWhiteSpace($pwUser1) -or [string]::IsNullOrWhiteSpace($pwUser2)) {
    Write-Output "[ERROR] Passwords (PW_USER1, PW_USER2) cannot be empty."
    exit 3
}

function Set-AccountPassword {
    param(
        [Parameter(Mandatory = $true)][string]$UserName,
        [Parameter(Mandatory = $true)][string]$PlainPassword
    )

    if (Get-Command Get-LocalUser -ErrorAction SilentlyContinue) {
        $acct = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
        if (-not $acct) {
            Write-Output "[WARN] Local user '$UserName' does not exist, skipping password change."
            return
        }
        $secPw = ConvertTo-SecureString -String $PlainPassword -AsPlainText -Force
        Set-LocalUser -Name $UserName -Password $secPw
        Write-Output "[OK] ${UserName}'s password updated."
        return
    }

    # Fallback via ADSI if Microsoft.PowerShell.LocalAccounts is unavailable
    try {
        $adsiUser = [ADSI]"WinNT://./$UserName,user"
        if (-not $adsiUser.Path) {
            Write-Output "[WARN] Local user '$UserName' does not exist, skipping password change."
            return
        }
        $adsiUser.SetPassword($PlainPassword)
        $adsiUser.SetInfo()
        Write-Output "[OK] ${UserName}'s password updated (ADSI)."
    } catch {
        Write-Output "[ERROR] Failed to update password for '${UserName}': $($_.Exception.Message)"
        exit 4
    }
}

Set-AccountPassword -UserName $user1.Trim() -PlainPassword $pwUser1
Set-AccountPassword -UserName $user2.Trim() -PlainPassword $pwUser2

Write-Output "[SUCCESS] Target Windows user passwords updated."
exit 0
