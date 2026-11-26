@echo off
rem MarketMuse / Market Signals V9 - start the app and open it in the browser.
rem   start_market_signals.bat             start (or just open the page if already running)
rem   start_market_signals.bat --dry-run   say what would happen, start nothing
setlocal
cd /d "%~dp0"
set "MS_URL=http://localhost:8003/"
set "MS_HEALTH=http://127.0.0.1:8003/api/health"

if /i "%~1"=="--open-when-ready" goto :open_when_ready

rem Already running? Then only open the page.
curl.exe -s -m 2 -o nul "%MS_HEALTH%"
if not errorlevel 1 (
    if /i "%~1"=="--dry-run" (echo Already running: would open %MS_URL% & exit /b 0)
    start "" "%MS_URL%"
    exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
    echo The Python environment was not found: "%~dp0.venv\Scripts\python.exe"
    echo The app cannot start without it.
    if /i not "%~1"=="--dry-run" pause
    exit /b 1
)
if not exist "run_server.py" (
    echo run_server.py was not found in "%~dp0"
    if /i not "%~1"=="--dry-run" pause
    exit /b 1
)

if /i "%~1"=="--dry-run" (
    echo Not running: would start ".venv\Scripts\python.exe run_server.py" in "%~dp0"
    echo and open %MS_URL% once %MS_HEALTH% answers.
    exit /b 0
)

title Market Signals V9 - the app runs while this window is open
echo Starting Market Signals V9...
echo The page opens in your browser as soon as the server is ready.
echo Keep this window open; closing it stops the app.
echo.

rem A second, minimised copy of this script waits for the server and opens the page.
start "" /min "%~f0" --open-when-ready

".venv\Scripts\python.exe" run_server.py

echo.
echo Market Signals V9 has stopped.
pause
exit /b 0

:open_when_ready
rem Wait up to three minutes for the server, then open the page.
for /l %%i in (1,1,90) do (
    curl.exe -s -m 2 -o nul "%MS_HEALTH%"
    if not errorlevel 1 (
        start "" "%MS_URL%"
        exit
    )
    timeout /t 2 /nobreak >nul
)
exit
