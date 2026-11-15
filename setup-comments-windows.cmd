@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run start-windows.cmd once to create the project environment, then retry.
  exit /b 1
)
echo Installing comment dependencies and Chromium. Internet access is required.
".venv\Scripts\python.exe" -m pip install -r tools\douyin_comments\requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 exit /b 1
echo Comment dependencies installed. See tools\douyin_comments\README.md for the experimental CLI.
