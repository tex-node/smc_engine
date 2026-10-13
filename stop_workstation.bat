@echo off
setlocal EnableExtensions
title SMC Engine Workstation Stop

rem Stops the workstation server by PORT OWNERSHIP (127.0.0.1:8765), not by
rem window title. Window-title matching is unreliable (the title drifts once
rem uvicorn writes to the console, and the window may be owned by Windows
rem Terminal) -- see tools\stop_workstation.ps1 for the full rationale.
rem Only the server's own process tree is stopped; never all python.exe.
rem Idempotent: exits 0 when nothing is listening on the port.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\stop_workstation.ps1" %*
set "RC=%errorlevel%"
endlocal & exit /b %RC%
