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
echo Checking backend readiness ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 40;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:5409/dm/settings' -TimeoutSec 2; if($r.StatusCode -eq 200){$ok=$true; break} } catch {}; Start-Sleep -Milliseconds 500 }; if(-not $ok){Write-Host '[ERROR] Backend did not respond on http://127.0.0.1:5409'; exit 1}"
if errorlevel 1 (
  echo.
  echo [ERROR] Backend failed to start. Check the "FlowOps Backend" window for details.
  echo.
  pause
  exit /b 1
)
echo [OK] Backend is ready.
netstat -ano | findstr /R /C:":5173 .*LISTENING" >nul
if errorlevel 1 (
  echo [2/2] Starting frontend ...
  start "FlowOps Frontend" "%~dp0run_frontend.bat"
) else (
  echo [2/2] Frontend already running on port 5173.
)
echo Checking frontend readiness ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 40;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:5173/' -TimeoutSec 2; if($r.StatusCode -eq 200){$ok=$true; break} } catch {}; Start-Sleep -Milliseconds 500 }; if(-not $ok){Write-Host '[ERROR] Frontend did not respond on http://127.0.0.1:5173'; exit 1}"
if errorlevel 1 (
  echo.
  echo [ERROR] Frontend failed to start. Check the "FlowOps Frontend" window for details.
  echo.
  pause
  exit /b 1
)
echo [OK] Frontend is ready.
echo.
start "" "http://localhost:5173/"
echo Done. Keep the two service windows open while using the app.
ping -n 5 127.0.0.1 >nul
exit /b 0

