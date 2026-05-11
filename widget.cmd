@echo off
rem Launches the AI Usage tray regardless of whether python is on PATH.
setlocal

set "APP_DIR=%~dp0"
set "TRAY_PY=%APP_DIR%tray.py"

rem 1) Prefer the windowless Python launcher.
where /q pyw.exe
if %ERRORLEVEL% == 0 (
    pyw "%TRAY_PY%" %*
    goto :eof
)

rem 2) Fall back to pythonw on PATH.
where /q pythonw.exe
if %ERRORLEVEL% == 0 (
    pythonw "%TRAY_PY%" %*
    goto :eof
)

rem 3) Hard-coded fallback to this machine's known install.
set "PYW_FALLBACK=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if exist "%PYW_FALLBACK%" (
    "%PYW_FALLBACK%" "%TRAY_PY%" %*
    goto :eof
)

echo Could not find pythonw.exe. Install Python 3.11+ or fix PATH.
exit /b 1
