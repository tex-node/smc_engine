@echo off
rem ============================================================
rem  SMC ENGINE WORKSTATION LAUNCHER
rem  All credential handling and server orchestration are in:
rem    tools\provision_mt5_credentials.ps1  (run once to set up)
rem    tools\load_mt5_credentials.ps1       (called here)
rem  No credentials are stored in this file.
rem ============================================================
cd /d "C:\smc_engine"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\load_mt5_credentials.ps1"
