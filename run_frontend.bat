@echo off
cd /d "%~dp0social-auto-upload-main\sau_frontend"
title YunCe Frontend (port 5173)
rem 默认只监听本机，避免局域网其他设备直接打开工作台。
rem 需要手机/另一台电脑访问时改成 npm run dev -- --host 0.0.0.0，
rem 并同时给后端设置 YUNCE_ACCESS_TOKEN。
npm run dev
echo.
echo Frontend stopped. Press any key to close.
pause >nul

