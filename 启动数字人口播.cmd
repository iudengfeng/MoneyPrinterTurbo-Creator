@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 正在打开创作工作台。首次使用会自动准备组件，请保持此窗口打开。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_creator.ps1"
if errorlevel 1 (
  echo.
  echo 启动未完成。请根据上方提示重试，或查看 storage\creator\logs 中的日志。
  pause
  exit /b 1
)
endlocal
