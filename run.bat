@echo off
rem Starts the IDE on Windows. First argument is the project folder (default: .).
setlocal

set PROJECT=%~1
if "%PROJECT%"=="" set PROJECT=.

where python >nul 2>nul
if errorlevel 1 (
	echo [ide] python was not found in PATH - install Python 3.9+ from python.org
	exit /b 1
)

rem Optional: point at your notion2api instance.
rem set MONOIDE_BASE_URL=http://127.0.0.1:8000/v1
rem set MONOIDE_API_KEY=your-key

python main.py "%PROJECT%" %2 %3 %4 %5 %6 %7 %8 %9
endlocal
