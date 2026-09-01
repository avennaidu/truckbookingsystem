@echo off
REM Faded Studio by Jay - booking site.
REM Double-click this to open the shop's booking system. Leave the black
REM window open while the shop is trading; closing it takes the site down.
cd /d "%~dp0.."
title Faded Studio booking
echo Starting the booking site...
echo.
echo   Customers   http://localhost:8080/
echo   Jay's diary http://localhost:8080/admin
echo.
start "" http://localhost:8080/admin
python -m barbershop serve --port 8080
pause
