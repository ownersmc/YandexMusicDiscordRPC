@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    py -m venv .venv
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto error

".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto error

powershell -ExecutionPolicy Bypass -File ".\build_release.ps1"
if errorlevel 1 goto error

goto end

:error
echo.
echo Build failed. Check the lines above.
pause
exit /b 1

:end
echo.
echo Build complete. See the release folder.
pause
