# Creates Desktop shortcuts for the Truck Booking Bot and the per-tower
# debug Chromes. Run once (via create_desktop_shortcuts.bat).
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell   = New-Object -ComObject WScript.Shell

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) {
    $chrome = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
}

function Make-Shortcut($name, $target, $icon) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop "$name.lnk"))
    $lnk.TargetPath = $target
    $lnk.WorkingDirectory = $here
    if ($icon) { $lnk.IconLocation = $icon }
    $lnk.Save()
    Write-Host "Created: $name"
}

# main launcher: starts the web UI and opens the browser
Make-Shortcut "Truck Booking Bot" (Join-Path $here "launch_bot.bat") `
    $(if (Test-Path $chrome) { "$chrome,0" } else { $null })

# one bot per tower - each opens its own Chrome, logs in to N4 and books
# that tower (needs the N4 login saved in the bot first)
foreach ($t in "109", "202", "203", "205") {
    Make-Shortcut "Truck Bot - Tower $t" (Join-Path $here "run_bot_$t.bat") `
        $(if (Test-Path $chrome) { "$chrome,0" } else { $null })
}

# one debug Chrome per tower - only needed when NO login is saved and you
# want to log in to N4 by hand
foreach ($t in "109", "202", "203", "205") {
    Make-Shortcut "N4 Chrome - Tower $t" (Join-Path $here "start_chrome_$t.bat") `
        $(if (Test-Path $chrome) { "$chrome,0" } else { $null })
}
Write-Host ""
Write-Host "Done. With your N4 login saved in the bot, daily use is either:"
Write-Host "  - 'Truck Booking Bot' -> tick the towers -> Start, or"
Write-Host "  - double-click 'Truck Bot - Tower <n>' for just that tower."
Write-Host "The 'N4 Chrome' shortcuts are only for logging in by hand."
