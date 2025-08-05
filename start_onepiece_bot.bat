@echo off
setlocal enabledelayedexpansion

REM === Navigate to the script directory ===
cd /d "%~dp0Onepiece"
echo Starting s.toBot...

REM === Required Python modules ===
set modules=selenium configparser

REM === Check Python installation ===
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not installed or not added to PATH.
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
        echo [ERROR] Failed to install modules. Please install manually.
        pause
        exit /b 1
    )
) else (
    echo [✓] All dependencies satisfied.
)

REM === Start Python Script ===
python s.toBot.py

pause
endlocal