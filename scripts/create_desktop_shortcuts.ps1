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

# one debug Chrome per tower (log in to N4 in each before starting bots)
foreach ($t in "109", "202", "203", "205") {
    Make-Shortcut "N4 Chrome - Tower $t" (Join-Path $here "start_chrome_$t.bat") `
        $(if (Test-Path $chrome) { "$chrome,0" } else { $null })
}
Write-Host ""
Write-Host "Done. Daily use: open the tower Chromes you need, log in to N4,"
Write-Host "then double-click 'Truck Booking Bot' and press Start on the page."
