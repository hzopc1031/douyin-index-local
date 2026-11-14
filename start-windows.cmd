@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python launcher not found. Install Python 3.12 from python.org and try again.
    exit /b 1
  )
  echo First launch: creating a local Python 3.12 environment.
  py -3.12 -m venv .venv
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -c "import requests, cryptography, webview, tzdata" >nul 2>&1
if errorlevel 1 (
  echo Installing project dependencies. Internet access is required.
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Check your network and retry.
    exit /b 1
  )
)
".venv\Scripts\python.exe" app.py
exit /b %errorlevel%
