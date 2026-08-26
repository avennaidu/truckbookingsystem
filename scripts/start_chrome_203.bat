@echo off
rem Debug Chrome for TOWER 203 (port 9224). Log in to N4 in this window,
rem open the Add Appointment screen, then start the bot / press Start
rem in the web UI.
set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME% set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
start "" %CHROME% --remote-debugging-port=9224 --user-data-dir="C:\navis-chrome-203"
