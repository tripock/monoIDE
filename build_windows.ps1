# Builds a single-file monoide.exe (python backend + bundled notion2api) with PyInstaller.
#
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1 -WithPty -NoConsole
#
# -WithPty    bundles pywinpty, giving the built-in terminal a real ConPTY
#             (arrow keys, ncurses, colours). Without it the terminal falls
#             back to line mode over pipes.
# -NoConsole  hides the console window of the exe itself.
#
# For the desktop app (Electron shell) use build_app.ps1 instead - it calls
# this script first and then packages electron/.

param(
	[switch]$WithPty,
	[switch]$NoConsole
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# NOTE: PowerShell unwraps single-element arrays on `return`, which used to turn
# @("C:\python.exe") into the *string* "C:\python.exe" and then $python[0] into
# the character "C" ('& : Имя "C" не распознано...'). We therefore return an
# ordered hashtable with the exe and its extra args kept apart.
function Find-Python {
	foreach ($candidate in @("python", "python3", "py")) {
		$found = Get-Command $candidate -ErrorAction SilentlyContinue
		if (-not $found) { continue }
		$exePath = if ($found.Source) { $found.Source } else { $found.Name }
		if ($candidate -eq "py") {
			return [ordered]@{ Exe = $exePath; Args = @("-3") }
		}
		return [ordered]@{ Exe = $exePath; Args = @() }
	}
	throw "python 3.9+ not found in PATH - install it from python.org, tick 'Add to PATH', re-open PowerShell and re-run"
}

$python = Find-Python
$exe = [string]$python.Exe
$pre = @($python.Args)

Write-Host "[build] python  : $exe $($pre -join ' ')"
if ($pre.Count -gt 0) { & $exe @pre --version } else { & $exe --version }
if ($LASTEXITCODE -ne 0) { throw "'$exe' is not a working python interpreter" }

$venv = Join-Path $PSScriptRoot ".build"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $vpy)) {
	Write-Host "[build] venv    : $venv"
	if ($pre.Count -gt 0) { & $exe @pre -m venv $venv } else { & $exe -m venv $venv }
	if (-not (Test-Path $vpy)) { throw "could not create the build venv at $venv" }
}

Write-Host "[build] deps    : pyinstaller$(if ($WithPty) { ' + pywinpty' })"
& $vpy -m pip install --disable-pip-version-check --quiet --upgrade pip
& $vpy -m pip install --disable-pip-version-check --quiet pyinstaller
if ($WithPty) { & $vpy -m pip install --disable-pip-version-check --quiet pywinpty }

foreach ($dir in @("dist", "build")) {
	if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}

if (-not (Test-Path (Join-Path $PSScriptRoot "vendor\notion2api\app\server.py"))) {
	Write-Warning "vendor\notion2api is missing - the exe will not be able to start the built-in API"
}

$env:MONOIDE_NOCONSOLE = if ($NoConsole) { "1" } else { "0" }
Write-Host "[build] running pyinstaller (this takes a minute)"
& $vpy -m PyInstaller --noconfirm --clean monoide.spec
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed with exit code $LASTEXITCODE" }

$out = Join-Path $PSScriptRoot "dist\monoide.exe"
if (-not (Test-Path $out)) { throw "expected $out to exist" }
$mb = [math]::Round((Get-Item $out).Length / 1MB, 1)

Write-Host ""
Write-Host "[build] done    : dist\monoide.exe ($mb MB)"
Write-Host "[build] usage   : .\dist\monoide.exe C:\path\to\project"
Write-Host "[build] the bundled notion2api starts automatically on 127.0.0.1:8000"
Write-Host "[build] desktop : run .\build_app.ps1 to get the Electron app with a folder picker"
