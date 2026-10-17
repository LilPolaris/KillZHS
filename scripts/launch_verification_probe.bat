@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0KillZHS.exe" (
  echo 请在解压后的发布目录中运行“验证实验模式.bat”。
  pause
  exit /b 1
)
echo 验证实验模式：请先退出旧版后台服务，再启动本模式。
echo 出现验证后，请在官网完成验证，再点击工作台的“我已完成网页验证”。
set "KILLZHS_VERIFY_PROBE=1"
start "" "%~dp0KillZHS.exe" --desktop
endlocal
