# Builds the Windows installer (Cool MP3 Player Setup.exe) with Inno Setup.
# The installer detects an existing install and upgrades it in place.
#
# Usage:  pwsh packaging\windows\build-installer.ps1
# Needs:  Inno Setup 6  (winget install JRSoftware.InnoSetup)
$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
Write-Host "Project root: $root" -ForegroundColor Cyan

# 1. Make sure the app .exe exists; build it first if it doesn't.
$exe = Join-Path $root "dist\windows\Cool MP3 Player.exe"
if (-not (Test-Path $exe)) {
    Write-Host "App .exe not found - building it first..." -ForegroundColor Cyan
    pwsh -NoProfile -File (Join-Path $PSScriptRoot "build.ps1")
}

# 2. Read the version straight from player.py so the installer matches the app.
$verMatch = Select-String -Path (Join-Path $root "player.py") `
                          -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $verMatch) { throw "Couldn't find APP_VERSION in player.py" }
$version = $verMatch.Matches[0].Groups[1].Value
Write-Host "App version: $version" -ForegroundColor Cyan

# 3. Locate the Inno Setup compiler (ISCC.exe).
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($p in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $iscc = $p; break }
    }
}
if (-not $iscc) {
    Write-Host "`nInno Setup compiler (ISCC.exe) not found." -ForegroundColor Yellow
    Write-Host "Install it, then re-run this script:" -ForegroundColor Yellow
    Write-Host "    winget install --id JRSoftware.InnoSetup -e" -ForegroundColor Yellow
    throw "ISCC.exe not found"
}
Write-Host "Using Inno Setup: $iscc" -ForegroundColor Cyan

# 4. Compile the installer (version passed in as a preprocessor define).
& $iscc "/DMyAppVersion=$version" (Join-Path $PSScriptRoot "installer.iss")

$setup = Join-Path $root "dist\windows\Cool MP3 Player Setup.exe"
if (Test-Path $setup) {
    $sizeMB = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host "`nDone -> $setup  ($sizeMB MB)" -ForegroundColor Green
} else {
    throw "Build finished but the installer was not found at $setup"
}
