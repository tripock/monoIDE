# Builds the desktop app: python backend exe + Electron shell.
#
#   powershell -ExecutionPolicy Bypass -File build_app.ps1              # portable app folder
#   powershell -ExecutionPolicy Bypass -File build_app.ps1 -Installer   # + NSIS installer
#
# The default path uses @electron/packager: no code-signing helpers, no
# symlinks, no admin rights. electron-builder (-Installer) has to unpack
# winCodeSign, which contains macOS symlinks - on Windows that only works with
# Developer Mode enabled or in an elevated shell, otherwise it dies with
# "Cannot create symbolic link ... libcrypto.dylib".

param(
	[switch]$WithPty,
	[switch]$SkipBackend,
	[switch]$Installer
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not $SkipBackend) {
	Write-Host "[app] step 1/3 - python backend (monoide.exe)"
	$backendScript = Join-Path $PSScriptRoot "build_windows.ps1"
	if ($WithPty) {
		& powershell -ExecutionPolicy Bypass -File $backendScript -WithPty -NoConsole
	} else {
		& powershell -ExecutionPolicy Bypass -File $backendScript -NoConsole
	}
	if ($LASTEXITCODE -ne 0) { throw "backend build failed" }
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
	throw "npm not found - install Node.js 18+ from nodejs.org, re-open PowerShell and re-run"
}

Set-Location (Join-Path $PSScriptRoot "electron")
Write-Host "[app] step 2/3 - npm install"
& npm install --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { throw "npm install failed" }

if ($Installer) {
	Write-Host "[app] step 3/3 - electron-builder (installer)"
	& npm run dist
	if ($LASTEXITCODE -ne 0) {
		Write-Warning "electron-builder failed. If it complains about 'Cannot create symbolic link ... .dylib',"
		Write-Warning "turn on Windows Developer Mode (Settings > System > For developers) or run this script as"
		Write-Warning "Administrator. Or simply use the default portable build: .\build_app.ps1"
		throw "electron-builder failed"
	}
	Write-Host ""
	Write-Host "[app] done - see electron\dist"
	return
}

Write-Host "[app] step 3/3 - electron-packager (portable app, no code signing)"
& npm run package
if ($LASTEXITCODE -ne 0) { throw "electron-packager failed" }

$appDir = Join-Path $PSScriptRoot "electron\dist\Mono IDE-win32-x64"
Write-Host ""
Write-Host "[app] done    : $appDir"
Write-Host "[app] run     : '$appDir\Mono IDE.exe'"
Write-Host "[app] installer (optional, needs Developer Mode): .\build_app.ps1 -SkipBackend -Installer"
