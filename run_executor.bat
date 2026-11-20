@echo off
cd /d "%~dp0social-auto-upload-main"
set PYTHONIOENCODING=utf-8
set FLOWOPS_PROCESS_ROLE=executor
title YunCe Publish Executor
"%~dp0venv\Scripts\python.exe" publish_executor.py
echo.
echo Publish executor stopped. Press any key to close.
pause >nul
