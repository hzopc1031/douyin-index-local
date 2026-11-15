@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python launcher not found. Install Python 3.12 from python.org and try again.
    exit /b 1
  )
  echo Creating a local Python 3.12 environment.
  py -3.12 -m venv .venv
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)"
if errorlevel 1 (
  echo The existing project environment must use Python 3.12. Create a compatible environment and retry.
  exit /b 1
)
echo Installing comment dependencies and Chromium. Internet access is required.
".venv\Scripts\python.exe" -m pip install -r tools\douyin_comments\requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 exit /b 1
echo Comment dependencies installed. See tools\douyin_comments\README.md for the experimental CLI.
