# Builds the Windows .exe for Cool MP3 Player (lean profile).
# Usage:  right-click → Run with PowerShell,  or:  pwsh packaging\windows\build.ps1
$ErrorActionPreference = "Stop"

# Repo root = two levels up from this script.
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
Write-Host "Project root: $root" -ForegroundColor Cyan

# 1. Make sure the build tools + lean runtime deps are present.
Write-Host "Installing build dependencies..." -ForegroundColor Cyan
python -m pip install --quiet --upgrade pyinstaller pygame Pillow mutagen numpy

# 2. Build using the spec. Artifact lands directly in dist\windows.
Write-Host "Building Cool MP3 Player.exe ..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm `
    --distpath "dist\windows" `
    --workpath "build\windows" `
    "packaging\windows\cool_mp3_player.spec"

$exe = Join-Path $root "dist\windows\Cool MP3 Player.exe"
if (Test-Path $exe) {
    $sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "`nDone -> $exe  ($sizeMB MB)" -ForegroundColor Green
} else {
    throw "Build finished but the .exe was not found at $exe"
}
