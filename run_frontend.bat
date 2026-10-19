@echo off
cd /d "%~dp0social-auto-upload-main\sau_frontend"
title YunCe Frontend (port 5173)
npm run dev -- --host 0.0.0.0
echo.
echo Frontend stopped. Press any key to close.
pause >nul

