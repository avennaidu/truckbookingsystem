@echo off
rem One-time setup: puts double-clickable shortcuts on your Desktop -
rem "Truck Booking Bot" (starts the UI) and one N4 Chrome per tower.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcuts.ps1"
pause
