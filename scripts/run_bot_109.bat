@echo off
title Truck Bot - Tower 109
rem Books tower 109 on its own Chrome (port from config debug_ports).
rem With the N4 login saved in config.json this needs no clicks at
rem all: it opens Chrome, logs in, sets the gate, the transaction
rem type and the trucking company, then camps for slots.
cd /d "%~dp0.."
python -m truckbot run --tower 109
pause
