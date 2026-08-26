@echo off
rem Starts the Truck Booking Bot web UI on http://localhost:8123
cd /d "%~dp0.."
python -m truckbot ui
pause
