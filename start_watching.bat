@echo off
setlocal enabledelayedexpansion
if "%BW_KEEP_SHELL%"=="" (
    set "BW_KEEP_SHELL=1"
    cmd /k ""%~f0""
    exit /b 0
)
set "BW_LOG=%~dp0SerienJunkie\bw_startup.log"
echo [BingeWatcher] Starting... > "%BW_LOG%"

REM === Navigate to the script directory ===
cd /d "%~dp0SerienJunkie"
if %ERRORLEVEL% NEQ 0 (
    echo [X] Failed to change directory to "%~dp0SerienJunkie".
    echo [X] Failed to change directory to "%~dp0SerienJunkie". >> "%BW_LOG%"
    goto :handle_error
)
echo Starting Binge Watching...
echo [=] Working directory: %CD% >> "%BW_LOG%"

REM === Required Python modules ===
set modules=selenium configparser

REM === Check Python installation ===
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [X] Python is missing or absent from PATH.
    echo [X] Python is missing or absent from PATH. >> "%BW_LOG%"
    goto :handle_error
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

if "%missing_modules%" NEQ "" (
    set "missing_modules=%missing_modules:~1%"
)

if "%missing_modules%"=="" (
    echo [=] All dependencies satisfied.
    echo [=] All dependencies satisfied. >> "%BW_LOG%"
) else (
    echo Installing missing modules:%missing_modules%
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install %missing_modules%
    if %ERRORLEVEL% NEQ 0 (
        echo [X] Failed to install modules. Please install manually.
        echo [X] Failed to install modules. Please install manually. >> "%BW_LOG%"
        goto :handle_error
    )
)

REM === Check for Chromaprint (fpcalc) ===
echo [i] Checking Chromaprint (fpcalc)... >> "%BW_LOG%"
where fpcalc >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [-] Chromaprint (fpcalc) missing. Attempting to install...
    echo [-] Chromaprint (fpcalc) missing. Attempting to install... >> "%BW_LOG%"
    where winget >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        winget install --id Chromaprint -e --silent >nul 2>&1
    )
    where choco >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        choco install chromaprint -y >nul 2>&1
    )
    where fpcalc >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [!] Chromaprint couldn't be installed automatically. Please install fpcalc manually.
        echo [!] Chromaprint couldn't be installed automatically. Please install fpcalc manually. >> "%BW_LOG%"
    ) else (
        echo [+] Chromaprint installed successfully.
        echo [+] Chromaprint installed successfully. >> "%BW_LOG%"
    )
) else (
    echo [+] Chromaprint (fpcalc) already installed.
    echo [+] Chromaprint (fpcalc) already installed. >> "%BW_LOG%"
)

REM === Check Tor setting from settings.json ===
set "USE_TOR=false"
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "try { $json = Get-Content -Raw 'settings.json' | ConvertFrom-Json; $value = $json.useTorProxy; if ($value -is [string]) { $value = $value.Trim().ToLower() -eq 'true' } else { $value = [bool]$value }; if ($value) { 'true' } else { 'false' } } catch { 'false' }"`) do (
    set "USE_TOR=%%a"
    goto :tor_setting_done
)
:tor_setting_done

if /i "%USE_TOR%"=="true" (
    echo [i] Tor DNS enabled in settings.json - starting Tor...
    
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
            if !waitcount! LSS 55 (
                goto waittorclose
            )
            echo [X] Port 9050 never became available after kill. Aborted execution.
            echo [X] Port 9050 never became available after kill. Aborted execution. >> "%BW_LOG%"
            goto :handle_error
        )
    )
    
    REM Start Tor now
    start "" /b "%TOR_PATH%" >nul 2>&1
    
    REM Wait until port 9050 is open (Tor has started)
    set /a waitcount=0
    :waittorstart
    timeout /t 1 >nul
    netstat -an | find "9050" >nul
    if %ERRORLEVEL% NEQ 0 (
        set /a waitcount+=1
        if !waitcount! LSS 30 goto waittorstart
        echo [X] port 9050 was never opened!
        echo [X] port 9050 was never opened! >> "%BW_LOG%"
        goto :handle_error
    )
    echo [+] Tor started successfully.
    echo [+] Tor started successfully. >> "%BW_LOG%"
)

REM === Start Python Script ===
set BW_DEBUG=1
python s.toBot.py
set EXITCODE=%ERRORLEVEL%
echo [i] Python exit code: %EXITCODE% >> "%BW_LOG%"

REM Immer pausieren, damit du die letzte Zeile siehst
echo.
echo [i] Python exit code: %EXITCODE%
pause

REM === Cleanup and exit depending on Python exit code ===
if %EXITCODE% EQU 0 (
    echo [=] BingeWatcher exited normally.
    if "%USE_TOR%"=="true" (
        echo [i] Cleaning up Tor process...
        taskkill /IM tor.exe /F >nul 2>&1
    )
    endlocal & exit /b 0
) else (
    echo [X] BingeWatcher exited with code %EXITCODE%.
    echo [X] BingeWatcher exited with code %EXITCODE%. >> "%BW_LOG%"
    if "%USE_TOR%"=="true" (
        echo [i] Cleaning up Tor process...
        taskkill /IM tor.exe /F >nul 2>&1
    )
    goto :handle_error
)

:handle_error
echo.
echo [!] Script aborted. Review the messages above.
echo [!] Script aborted. Review the messages above. >> "%BW_LOG%"
echo [i] Log saved to: %BW_LOG%
pause
endlocal & exit /b 1
