@echo off
cd /d "%~dp0"
title 关闭 HuggingFace 下载器

if not exist "app\stop_server.ps1" (
  echo 找不到 app\stop_server.ps1，请确认文件夹完整。
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "app\stop_server.ps1"

echo 这个窗口 3 秒后自动关闭。
ping -n 4 127.0.0.1 >nul
exit /b 0