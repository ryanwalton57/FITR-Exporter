@echo off
setlocal
cd /d "%~dp0"

echo.
echo FITR Workout Exporter
echo =====================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo The local Python environment was not found.
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)

echo Enter dates as YYYY-MM-DD only.
echo Start date should be the older/earlier date.
echo End date should be the newer/later date.
echo Example start date: 2025-09-15
echo Example end date:   2026-08-04
echo.

set /p START_DATE=Start date:
set /p END_DATE=End date:

if "%START_DATE%"=="" (
  echo Start date is required.
  pause
  exit /b 1
)

if "%END_DATE%"=="" (
  echo End date is required.
  pause
  exit /b 1
)

echo.
echo Chromium will open. Log in to FITR if asked.
echo Return to this window and press Enter when the FITR calendar is visible.
echo.

".venv\Scripts\python.exe" fitr_export.py --start-date %START_DATE% --end-date %END_DATE%

echo.
echo Done. Look in fitr_output\export for the newest timestamped folder.
echo Open workouts.md for a readable report or workouts.csv for Excel.
echo.
pause
