@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.12 or 3.13 is required. Install Python with Tcl/Tk and add it to PATH.
    pause
    exit /b 1
)
python bootstrap.py %*
if errorlevel 1 (
    echo.
    echo ScriptTTS could not start. See the message above and data\reader.log.
    pause
)
