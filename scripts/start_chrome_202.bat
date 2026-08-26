@echo off
rem Debug Chrome for TOWER 202 (port 9223). Log in to N4 in this window,
rem open the Add Appointment screen, then start the bot / press Start
rem in the web UI.
set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME% set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
start "" %CHROME% --remote-debugging-port=9223 --user-data-dir="C:\navis-chrome-202"
