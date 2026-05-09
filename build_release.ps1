$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "py"
}

$ReleaseDir = Join-Path $Root "release"
$DistDir = Join-Path $Root "dist"
$BuildDir = Join-Path $Root "build"

foreach ($Path in @($ReleaseDir, $DistDir, $BuildDir)) {
    $ResolvedRoot = (Resolve-Path $Root).Path
    $Parent = Split-Path -Parent $Path
    if ((Resolve-Path $Parent).Path -ne $ResolvedRoot) {
        throw "Refusing to clean outside workspace: $Path"
    }
    if (Test-Path $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt pyinstaller

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --console `
    --name "YandexMusicDiscordRPC" `
    --hidden-import "winrt.windows.foundation" `
    --hidden-import "winrt.windows.foundation.collections" `
    --hidden-import "winrt.windows.media.control" `
    --hidden-import "winrt.windows.storage.streams" `
    "yd_discord_presence.py"

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
Copy-Item -LiteralPath (Join-Path $DistDir "YandexMusicDiscordRPC.exe") -Destination $ReleaseDir
Copy-Item -LiteralPath (Join-Path $Root "README_START.txt") -Destination $ReleaseDir
Copy-Item -LiteralPath (Join-Path $Root "config.example.json") -Destination (Join-Path $ReleaseDir "config.example.json")

$ZipPath = Join-Path $Root "YandexMusicDiscordRPC-portable.zip"
if (Test-Path $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $ReleaseDir "*") -DestinationPath $ZipPath

Write-Host "Release files:"
Get-ChildItem -LiteralPath $ReleaseDir
Write-Host "Archive: $ZipPath"
