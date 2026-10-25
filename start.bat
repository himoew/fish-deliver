@echo off
chcp 65001 >nul
title fish deliver - 肥鱼快递
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 lan-drop.py %*
) else (
  python lan-drop.py %*
)

if errorlevel 1 (
  echo.
  echo 启动失败：请确认已安装 Python 3.8 或更高版本。
  echo 也可以在这个目录里手动运行：python lan-drop.py
  pause
)
