@echo off
cd /d "%~dp0social-auto-upload-main"
set PYTHONIOENCODING=utf-8
title YunCe Backend (port 5409)
"%~dp0venv\Scripts\python.exe" sau_backend.py
echo.
echo Backend stopped. Press any key to close.
pause >nul

