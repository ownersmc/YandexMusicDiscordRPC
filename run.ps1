$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv")) {
    py -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path "config.json")) {
    Copy-Item "config.example.json" "config.json"
    Write-Host "Created config.json. Put your Discord Application ID into discord_client_id, then run this script again."
    exit 1
}

& ".\.venv\Scripts\python.exe" ".\yd_discord_presence.py"
