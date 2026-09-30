@echo off
setlocal EnableExtensions
title SMC Engine Workstation Launcher

rem ============================================================
rem  SMC ENGINE WORKSTATION LAUNCHER
rem  Starts the local analysis workstation and opens the browser.
rem  This launcher contains NO credentials and NEVER executes
rem  anything: it only starts the server and opens the UI.
rem ============================================================

set "REPO=C:\smc_engine"
set "SMC_HOST=127.0.0.1"
if not defined SMC_GUI_PORT set "SMC_GUI_PORT=8765"
set "PORT=%SMC_GUI_PORT%"
set "URL=http://%SMC_HOST%:%PORT%/"
set "VENV_PY=%REPO%\.venv\Scripts\python.exe"
set "SRV_TITLE=SMC Engine Workstation Server"

echo ========================================
echo        SMC ENGINE WORKSTATION
echo ========================================
echo.
echo Repository: %REPO%
echo Server:     %URL%
echo.

if not exist "%REPO%\" goto :err_repo
cd /d "%REPO%" 1>nul 2>nul || goto :err_repo

if not exist "%VENV_PY%" goto :err_venv
echo Environment:
echo   .venv found
echo.

echo Checking workstation...
curl -s -o nul --max-time 3 "%URL%"
if %errorlevel% equ 0 goto :already
echo Not running yet.
echo.

echo Starting server...
start "%SRV_TITLE%" /D "%REPO%" "%VENV_PY%" -m uvicorn smc_engine.web.api:create_app --factory --host %SMC_HOST% --port %PORT%

echo Waiting for workstation:
set /a TRIES=0
:waitloop
ping -n 2 127.0.0.1 >nul 2>nul
curl -s -o nul --max-time 2 "%URL%" && goto :ready
set /a TRIES+=1
if %TRIES% geq 60 goto :err_timeout
echo   .
goto :waitloop

:ready
echo   [OK]
echo.
echo Server ready. Opening browser...
start "" "%URL%"
echo.
echo ========================================
echo   SMC ENGINE WORKSTATION READY
echo ========================================
echo.
echo URL: %URL%
echo.
echo Do not close the server window while using
echo the workstation.
echo.
endlocal
exit /b 0

:already
echo ========================================
echo   SMC ENGINE already running.
echo   Opening workstation...
echo ========================================
start "" "%URL%"
endlocal
exit /b 0

:err_repo
echo ========================================
echo SMC ENGINE
echo ERROR: Repository directory not found.
echo.
echo Expected:
echo   %REPO%
echo ========================================
endlocal
exit /b 1

:err_venv
echo ========================================
echo SMC ENGINE
echo ERROR: Python virtual environment not found.
echo.
echo Expected:
echo   %VENV_PY%
echo.
echo The launcher will not create or install an
echo environment automatically.
echo ========================================
endlocal
exit /b 1

:err_timeout
echo ========================================
echo SMC ENGINE
echo ERROR: Workstation did not become ready.
echo.
echo Check the server window for details.
echo.
echo The browser was not launched.
echo ========================================
endlocal
exit /b 2
