@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\creator\setup_creator.ps1" -CheckOnly -Offline
echo.
pause
endlocal
