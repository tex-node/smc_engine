#Requires -Version 5.1
<#
.SYNOPSIS
    Loads DPAPI-encrypted MT5 credentials and starts the SMC Engine workstation.
    Called automatically by start_workstation.bat.  Never prints the secret.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Security

$REPO        = "C:\smc_engine"
$STORE_PATH  = "$env:APPDATA\SMC_ENGINE\mt5_credentials.dpapi"
$ENTROPY     = [System.Text.Encoding]::UTF8.GetBytes("SMC_ENGINE_MT5_V1")
$HOST_ADDR   = "127.0.0.1"
$PORT        = if ($env:SMC_GUI_PORT) { $env:SMC_GUI_PORT } else { "8765" }
$URL         = "http://$HOST_ADDR`:$PORT/"
$VENV_PY     = "$REPO\.venv\Scripts\python.exe"
$SRV_TITLE   = "SMC Engine Workstation Server"

$AUTH_LOGIN  = "477217728"
$AUTH_SERVER = "Exness-MT5Trial9"

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "  ERROR: $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "  SMC ENGINE WORKSTATION" -ForegroundColor Cyan
Write-Host "  ----------------------" -ForegroundColor Cyan
Write-Host ""

# --- Pre-flight checks -------------------------------------------------------
if (-not (Test-Path $REPO)) { Fail "Repository not found: $REPO" }
if (-not (Test-Path $VENV_PY)) { Fail "Python environment not found: $VENV_PY  Run setup first." }

if (-not (Test-Path $STORE_PATH)) {
    Write-Host "  No credential store found -- launching provisioning..." -ForegroundColor Yellow
    Write-Host ""
    & "$REPO\tools\provision_mt5_credentials.ps1"
    if (-not (Test-Path $STORE_PATH)) {
        Fail "Provisioning did not create a credential store. Cannot start."
    }
}

# --- Decrypt -----------------------------------------------------------------
try {
    $blob      = [System.IO.File]::ReadAllBytes($STORE_PATH)
    $plain_raw = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $blob, $ENTROPY,
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $json  = [System.Text.Encoding]::UTF8.GetString($plain_raw)
    $creds = $json | ConvertFrom-Json
} catch {
    Fail "Failed to decrypt credential store: $_`n  Store may be corrupt or owned by a different Windows user.`n  Re-run tools\provision_mt5_credentials.ps1 to re-provision."
}

if (-not $creds.login -or -not $creds.server -or -not $creds.secret) {
    Fail "Credential store is missing required fields. Re-run provision_mt5_credentials.ps1."
}

# --- Identity pre-check (fail closed before touching MT5) -------------------
if ($creds.login -ne $AUTH_LOGIN -or $creds.server -notmatch [regex]::Escape($AUTH_SERVER)) {
    Write-Host ""
    Write-Host "  ERROR: Credential store contains an unauthorized account." -ForegroundColor Red
    Write-Host "    Stored login  : $($creds.login)   (required: $AUTH_LOGIN)" -ForegroundColor Red
    Write-Host "    Stored server : $($creds.server)   (required: $AUTH_SERVER)" -ForegroundColor Red
    Write-Host ""
    $creds.secret = $null; [System.GC]::Collect()
    exit 1
}

# --- Self-diagnostic table (no secret shown) ---------------------------------
Write-Host "  Credentials :   CONFIGURED (DPAPI)"     -ForegroundColor Green
Write-Host "  MT5 Account :   $($creds.login)"
Write-Host "  Server      :   $($creds.server)"
Write-Host "  Account Mode:   DEMO (verified at connect)"
Write-Host "  Identity Gate:  PRE-CHECK PASSED ($AUTH_LOGIN @ $AUTH_SERVER)"
Write-Host "  Live Execution: DISABLED"
Write-Host "  Environment :   .venv"
Write-Host "  API         :   $URL"
Write-Host ""

# --- Already running? --------------------------------------------------------
try {
    $r = Invoke-WebRequest -Uri "${URL}api/status" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
    Write-Host "  Workstation already running -- opening browser..." -ForegroundColor Green
    Start-Process $URL
    $creds.secret = $null; [System.GC]::Collect()
    exit 0
} catch {
    Write-Host "  Server not running -- starting now..."
    Write-Host ""
}

# --- Launch server (pass credentials via env, NOT command line) --------------
$env:MT5_LOGIN    = $creds.login
$env:MT5_SERVER   = $creds.server
$env:MT5_PASSWORD = $creds.secret

$uvicornArgs = "/K title `"$SRV_TITLE`" && `"$VENV_PY`" -m uvicorn smc_engine.web.api:create_app --factory --host $HOST_ADDR --port $PORT"
$proc = Start-Process cmd.exe `
    -ArgumentList $uvicornArgs `
    -WorkingDirectory $REPO `
    -WindowStyle Normal `
    -PassThru

# Wipe from this session immediately -- the child window retains its own copy
$env:MT5_PASSWORD = $null
$creds.secret     = $null
[System.GC]::Collect()

if ($null -eq $proc) { Fail "Failed to start server process." }
Write-Host "  Server window launched (PID $($proc.Id))"

# --- Poll for readiness ------------------------------------------------------
Write-Host "  Waiting for workstation  " -NoNewline
$maxTries = 60
$tries    = 0
$ready    = $false
while ($tries -lt $maxTries) {
    Start-Sleep -Seconds 2
    try {
        Invoke-WebRequest -Uri "${URL}api/status" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop | Out-Null
        $ready = $true
        break
    } catch {
        $tries++
        Write-Host "." -NoNewline
        if ($proc.HasExited) {
            Write-Host ""
            Fail "Server process exited (code $($proc.ExitCode)) before becoming ready. Check the server window."
        }
    }
}

if (-not $ready) {
    Write-Host ""
    Write-Host ""
    Fail "Workstation did not become ready after $maxTries attempts. Check the server window."
}

Write-Host "  [OK]"
Write-Host ""

# --- Post-connect identity verification via API ------------------------------
try {
    $resp   = Invoke-WebRequest -Uri "${URL}api/status" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    $status = $resp.Content | ConvertFrom-Json

    $apiLogin  = if ($status.account.login)           { $status.account.login }           else { "UNKNOWN" }
    $apiServer = if ($status.account.server)          { $status.account.server }          else { "UNKNOWN" }
    $apiMode   = if ($status.account_mode)            { $status.account_mode }            else { "UNKNOWN" }
    $apiIdent  = if ($status.account_identity)        { $status.account_identity }        else { "UNKNOWN" }
    $apiLive   = if ($null -ne $status.live_execution_enabled) { $status.live_execution_enabled } else { "UNKNOWN" }

    Write-Host "  ========================================"
    Write-Host "    SMC ENGINE WORKSTATION READY"           -ForegroundColor Green
    Write-Host "  ========================================"
    Write-Host ""
    Write-Host "  Account :       $apiLogin"
    Write-Host "  Server  :       $apiServer"
    Write-Host "  Mode    :       $apiMode"
    Write-Host "  Identity Gate:  $apiIdent"
    Write-Host "  Live Execution: $(if ($apiLive -eq $false) { 'DISABLED' } else { $apiLive })"
    Write-Host "  URL     :       $URL"
    Write-Host ""

    if ($apiIdent -ne "AUTHORIZED") {
        Write-Host "  WARNING: Identity gate is NOT AUTHORIZED. Check the server window." -ForegroundColor Yellow
    }
    if ($apiLive -eq $true) {
        Write-Host "  WARNING: Live execution is not disabled!" -ForegroundColor Red
    }
} catch {
    Write-Host "  Server ready but /api/status unreachable: $_" -ForegroundColor Yellow
}

# --- Open browser ------------------------------------------------------------
Start-Process $URL
Write-Host "  Browser opened: $URL"
Write-Host ""
