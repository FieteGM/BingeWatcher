@echo off
setlocal enabledelayedexpansion

REM === Navigate to the script directory ===
cd /d "%~dp0SerienJunkie"
echo Starting Binge Watching...
echo Initializing Tor...

REM === Start Tor process (hidden window) ===
set TOR_PATH=%~dp0SerienJunkie\Browser\TorBrowser\Tor\tor.exe

REM Check if Tor is already running (Port 9050 in use)
netstat -an | find "9050" >nul
if %ERRORLEVEL% EQU 0 (
    echo [?] Tor seems to be running already. Trying to Kill and restart the process...
    taskkill /IM tor.exe /F >nul 2>&1
    REM Wait until port 9050 is truly free
    set /a waitcount=0
    :waittorclose
    timeout /t 1 >nul
    netstat -an | find "9050" >nul
    if %ERRORLEVEL% EQU 0 (
        set /a waitcount+=1
        if !waitcount! LSS 55 goto waittorclose
        echo [X] Port 9050 did not become available after kill. Aborted execution.
        pause
        exit /b 1
    )
)

REM Start goal now
start "" /b "%TOR_PATH%" >nul 2>&1

REM Wait until port 9050 is open (Tor has started)
set /a waitcount=0
:waittorstart
timeout /t 1 >nul
netstat -an | find "9050" >nul
if %ERRORLEVEL% NEQ 0 (
    set /a waitcount+=1
    if !waitcount! LSS 30 goto waittorstart
    echo [X] port 9050 was not opened!
    pause
    exit /b 1
)

REM === Required Python modules ===
set modules=selenium configparser

REM === Check Python installation ===
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [X] Python is not installed or not added to PATH.
    pause
    exit /b 1
)

REM === Check and install missing modules ===
set "missing_modules="

for %%m in (%modules%) do (
    python -c "import %%m" >nul 2>&1
    if !ERRORLEVEL! NEQ 0 (
        echo [-] Missing Python module: %%m
        set "missing_modules=!missing_modules! %%m"
    ) else (
        echo [+] Python module '%%m' already installed.
    )
)

if not "!missing_modules!"=="" (
    echo Installing missing modules:!missing_modules!
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install !missing_modules!
    if !ERRORLEVEL! NEQ 0 (
        echo [X] Failed to install modules. Please install manually.
        pause
        exit /b 1
    )
) else (
    echo [=] All dependencies satisfied.
)

REM === Start Python Script ===
python s.toBot.py

pause

REM === Kill Tor process (optional) ===
taskkill /IM tor.exe /F >nul 2>&1

endlocal