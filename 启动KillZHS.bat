@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" killzhs.py --desktop
) else (
  python killzhs.py --desktop
  if errorlevel 1 pause
)
