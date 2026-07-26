@echo off
rem Dev launcher for the desktop app: no build step, just Electron + python.
setlocal
cd /d "%~dp0electron"

where npm >nul 2>nul
if errorlevel 1 (
	echo [app] Node.js 18+ was not found in PATH - install it from nodejs.org
	exit /b 1
)

if not exist "node_modules\electron" (
	echo [app] first run: installing Electron ...
	call npm install --no-audit --no-fund || exit /b 1
)

call npm start
endlocal
