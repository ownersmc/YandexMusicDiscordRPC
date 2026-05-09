@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    py -m venv .venv
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto error

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error

if not exist "config.json" (
    copy "config.example.json" "config.json" >nul
    echo Created config.json. Put your Discord Application ID into discord_client_id, then run this file again.
    goto error
)

".venv\Scripts\python.exe" "yd_discord_presence.py"
if errorlevel 1 goto error

goto end

:error
echo.
echo The client stopped because of an error. Send the lines above if you need help.
pause

:end
