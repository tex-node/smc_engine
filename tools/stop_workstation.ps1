#Requires -Version 5.1
<#
.SYNOPSIS
    Stops the SMC Engine workstation server by PORT OWNERSHIP.

.DESCRIPTION
    The server's stable identity is its TCP listener on 127.0.0.1:<port>, not its
    console window. Window-title matching (`taskkill /FI "WINDOWTITLE eq ..."`)
    is unreliable: the title drifts once uvicorn writes to the console, and the
    window may be owned by Windows Terminal rather than the cmd.exe that started
    the server. This script instead:

      1. finds the PID(s) listening on the port
         (Get-NetTCPConnection, with a netstat fallback);
      2. walks ParentProcessId up (bounded) to the launcher cmd.exe
         (identified by its command line);
      3. taskkill /T /F on that launcher so the whole tree dies together
         (fallback: kill the port owner directly with /T /F);
      4. waits until the port is free, then reports.

    Idempotent: when nothing owns the port it prints "not running" and exits 0.
    Never kills python.exe globally — only the server's own process tree.

.PARAMETER Port
    Listener port. Defaults to $env:SMC_GUI_PORT, else 8765 (mirrors the launcher).

.PARAMETER MaxHops
    Maximum parent hops to walk (loop guard). Default 5.

.PARAMETER TimeoutSeconds
    How long to wait for the port to free before reporting failure. Default 10.
#>
[CmdletBinding()]
param(
    [int]$Port = $(if ($env:SMC_GUI_PORT) { [int]$env:SMC_GUI_PORT } else { 8765 }),
    [int]$MaxHops = 5,
    [int]$TimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"

function Get-PortOwnerPid([int]$p) {
    $owners = @()
    try {
        $owners = @(Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop |
                    ForEach-Object { [int]$_.OwningProcess })
    } catch { }
    if ($owners.Count -eq 0) {
        # Fallback for hosts where the NetTCPConnection cmdlet is unavailable.
        foreach ($line in (& netstat -ano | & findstr ":$p" | & findstr "LISTENING")) {
            $tokens = ($line.ToString().Trim() -split '\s+')
            if ($tokens.Count -ge 5) { $owners += [int]$tokens[-1] }
        }
    }
    return @($owners | Sort-Object -Unique)
}

function Get-LauncherChain([int]$StartPid, [int]$Max) {
    # Walk ParentProcessId upward; return the chain ending at the launcher
    # cmd.exe, or $null if no launcher is found within the bound.
    $chain = @()
    $cur = $StartPid
    for ($i = 0; $i -lt $Max; $i++) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
        if ($null -eq $proc) { break }
        $chain += $proc
        $ppid = [int]$proc.ParentProcessId
        if ($ppid -le 0) { break }
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$ppid" -ErrorAction SilentlyContinue
        if ($null -eq $parent) { break }
        if ($parent.Name -ieq "cmd.exe" -and
            $parent.CommandLine -match "SMC Engine Workstation Server|uvicorn\s+smc_engine") {
            $chain += $parent
            return ,$chain
        }
        $cur = $ppid
    }
    return $null
}

$owners = @(Get-PortOwnerPid $Port)
if ($owners.Count -eq 0) {
    Write-Host "No workstation server is listening on port $Port - nothing to stop."
    exit 0
}

Write-Host "Port $Port is owned by PID(s): $($owners -join ', ')"

foreach ($ownerPid in $owners) {
    $chain = Get-LauncherChain $ownerPid $MaxHops
    if ($chain) {
        $launcher = $chain[-1]
        $shape = ($chain | ForEach-Object { "$($_.Name)($($_.ProcessId))" }) -join " -> "
        Write-Host "Process chain: $shape"
        Write-Host "Stopping launcher tree: $($launcher.Name) PID $($launcher.ProcessId) (/T /F)"
        & taskkill /PID $launcher.ProcessId /T /F | Out-Null
    } else {
        Write-Host "No launcher parent found; stopping port owner PID $ownerPid (/T /F)"
        & taskkill /PID $ownerPid /T /F | Out-Null
    }
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (@(Get-PortOwnerPid $Port).Count -eq 0) {
        Write-Host "Workstation server stopped; port $Port is free."
        exit 0
    }
}

$still = @(Get-PortOwnerPid $Port)
Write-Host "ERROR: port $Port is still in use by PID(s): $($still -join ', ')"
exit 1
