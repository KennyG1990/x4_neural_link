@echo off
setlocal
cd /d "%~dp0"
title Neural Link - Deploy + Watch
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Deploy-And-Restart.ps1"
echo.
echo Watch mode exited. Press any key to close...
pause >nul
