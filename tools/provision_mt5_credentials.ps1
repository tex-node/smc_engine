#Requires -Version 5.1
<#
.SYNOPSIS
    One-time MT5 credential provisioning for SMC Engine.
    Encrypts credentials with Windows DPAPI and stores them under %APPDATA%.
    Run this once before using start_workstation.bat.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security

$STORE_PATH = "$env:APPDATA\SMC_ENGINE\mt5_credentials.dpapi"
$ENTROPY    = [System.Text.Encoding]::UTF8.GetBytes("SMC_ENGINE_MT5_V1")

Write-Host ""
Write-Host "========================================"
Write-Host "  SMC ENGINE -- Credential Provisioning"
Write-Host "========================================"
Write-Host ""

if (Test-Path $STORE_PATH) {
    Write-Host "  Existing credentials found at:"
    Write-Host "  $STORE_PATH"
    Write-Host ""
    $ans = Read-Host "  Overwrite? (y/N)"
    if ($ans.Trim().ToLower() -ne 'y') {
        Write-Host "  Cancelled. Existing credentials unchanged."
        Write-Host ""
        exit 0
    }
    Write-Host ""
}

Write-Host "  Press Enter to accept the default value shown in brackets."
Write-Host ""

$DEFAULT_LOGIN  = "477217728"
$DEFAULT_SERVER = "Exness-MT5Trial9"

$rawLogin = Read-Host "  MT5 Login [$DEFAULT_LOGIN]"
$login    = if ([string]::IsNullOrWhiteSpace($rawLogin))  { $DEFAULT_LOGIN }  else { $rawLogin.Trim() }

$rawServer = Read-Host "  MT5 Server [$DEFAULT_SERVER]"
$server    = if ([string]::IsNullOrWhiteSpace($rawServer)) { $DEFAULT_SERVER } else { $rawServer.Trim() }

$securePwd = Read-Host "  MT5 Account credential (not echoed)" -AsSecureString

$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePwd)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($plain)) {
    Write-Host ""
    Write-Host "  ERROR: Entry cannot be empty." -ForegroundColor Red
    $plain = $null; [System.GC]::Collect()
    exit 1
}

$payload = [ordered]@{ login = $login; server = $server; secret = $plain }
$json    = $payload | ConvertTo-Json -Compress
$raw     = [System.Text.Encoding]::UTF8.GetBytes($json)

$blob = [System.Security.Cryptography.ProtectedData]::Protect(
    $raw, $ENTROPY,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)

# Wipe plaintext from memory before writing
$plain = $null; $json = $null
[Array]::Clear($raw, 0, $raw.Length)
[System.GC]::Collect()

$dir = Split-Path $STORE_PATH
if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
[System.IO.File]::WriteAllBytes($STORE_PATH, $blob)

Write-Host ""
Write-Host "  ======================================"
Write-Host "  Credentials stored (DPAPI encrypted)."
Write-Host "  ======================================"
Write-Host ""
Write-Host "  Login  :  $login"
Write-Host "  Server :  $server"
Write-Host "  Store  :  $STORE_PATH"
Write-Host ""
Write-Host "  Run start_workstation.bat to launch."
Write-Host ""
