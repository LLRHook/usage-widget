# Installs or removes the AI Usage tray from the current user's logon apps.
# Usage:
#   pwsh .\install-startup.ps1            # install and launch
#   pwsh .\install-startup.ps1 -Uninstall # remove and stop

[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$trayPy = Join-Path $root 'tray.py'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = 'AI Usage Tray'

if (-not (Test-Path $trayPy)) {
    throw "tray.py not found at $trayPy"
}

function Find-Pythonw {
    $cmd = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if ($cmd) { return $cmd }

    $where = (& where.exe pythonw.exe 2>$null | Select-Object -First 1)
    if ($where) { return $where }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe",
        "$env:ProgramFiles\Python312\pythonw.exe",
        "$env:ProgramFiles\Python311\pythonw.exe"
    )
    return ($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
}

function Remove-LegacyShortcuts {
    $startup = [Environment]::GetFolderPath('Startup')
    foreach ($name in @('AI Usage Tray.lnk', 'AI Usage Widget.lnk', 'AI Usage Live Refresh.lnk')) {
        $path = Join-Path $startup $name
        if (Test-Path $path) {
            Remove-Item $path -Force
            Write-Host "Removed legacy startup shortcut: $path"
        }
    }
}

Remove-LegacyShortcuts

if ($Uninstall) {
    if (Get-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $runKey -Name $runName
        Write-Host "Removed startup Run entry: $runName"
    } else {
        Write-Host "No startup Run entry named $runName."
    }

    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'tray.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    return
}

$pyw = Find-Pythonw
if (-not $pyw) {
    throw "Could not locate pythonw.exe. Install Python 3.11+ and re-run."
}

$command = "`"$pyw`" `"$trayPy`""
New-Item -Path $runKey -Force | Out-Null
Set-ItemProperty -Path $runKey -Name $runName -Value $command

Write-Host "Installed startup Run entry:"
Write-Host "  $runName -> $command"
Write-Host "Launching tray now..."
Start-Process -FilePath $pyw -ArgumentList "`"$trayPy`"" -WorkingDirectory $root -WindowStyle Hidden
