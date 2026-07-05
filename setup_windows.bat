@echo off
setlocal
cd /d "%~dp0"

echo.
echo FITR Exporter - first-time setup
echo =================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  echo Install Python from https://www.python.org/downloads/windows/
  echo During install, check "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create the Python environment.
    pause
    exit /b 1
  )
)

echo Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Package install failed.
  pause
  exit /b 1
)

echo Installing Playwright Chromium browser...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo Playwright browser install failed.
  pause
  exit /b 1
)

echo.
echo Setup complete. You can now run export_workouts.bat.
echo.
pause
