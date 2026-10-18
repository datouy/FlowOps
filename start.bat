@echo off
setlocal
cd /d "%~dp0"
title FlowOps - Launcher
echo ============================================
echo   FlowOps - starting services
echo ============================================
echo.
netstat -ano | findstr /R /C:":5409 .*LISTENING" >nul
if errorlevel 1 (
  echo [1/2] Starting backend ...
  start "FlowOps Backend" "%~dp0run_backend.bat"
) else (
  echo [1/2] Backend already running on port 5409.
)
netstat -ano | findstr /R /C:":5173 .*LISTENING" >nul
if errorlevel 1 (
  echo [2/2] Starting frontend ...
  start "FlowOps Frontend" "%~dp0run_frontend.bat"
) else (
  echo [2/2] Frontend already running on port 5173.
)
echo.
echo Waiting for services to become ready ...
ping -n 9 127.0.0.1 >nul
start "" "http://localhost:5173/"
echo Done. Keep the two service windows open while using the app.
ping -n 5 127.0.0.1 >nul
exit /b 0

