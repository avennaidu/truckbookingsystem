@echo off
rem Usage: run_tower.bat 109   (or 202 / 203 / 205)
cd /d "%~dp0.."
python -m truckbot run --tower %1
pause
