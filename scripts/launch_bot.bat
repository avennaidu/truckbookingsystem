@echo off
title Truck Booking Bot
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH.
  echo Install it from https://www.python.org/downloads/ - tick "Add to PATH".
  pause
  exit /b 1
)

rem first run: install the one dependency
python -c "import playwright" 2>nul
if errorlevel 1 (
  echo First run - installing requirements...
  python -m pip install -r requirements.txt
)

rem first run: create a local config from the example
if not exist config.json copy config.example.json config.json >nul

echo Starting the Truck Booking Bot UI (the page opens by itself)...
python -m truckbot ui
pause
