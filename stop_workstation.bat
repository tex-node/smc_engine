@echo off
setlocal EnableExtensions
title SMC Engine Workstation Stop

rem Stops ONLY the workstation server console started by
rem start_workstation.bat (identified by its unique window title).
rem Never kills python.exe globally.

echo Looking for the SMC Engine workstation server window...
taskkill /FI "WINDOWTITLE eq SMC Engine Workstation Server*" /T >nul 2>nul
if %errorlevel% equ 0 goto :stopped

echo No workstation server window found.
echo.
echo If the server was started from a normal terminal,
echo close that console window to stop it.
endlocal
exit /b 0

:stopped
echo SMC Engine workstation server stopped.
endlocal
exit /b 0
