@echo off
setlocal EnableDelayedExpansion

title Online Food Shop

echo ======================================================================
echo                  Online Food Shop - Launcher
echo ======================================================================
echo.

rem 1. Navigate to script directory to ensure correct working directory
cd /d "%~dp0"

rem 2. Verify working directory contains project files
if not exist "run.py" (
    echo [ERROR] start_shop.bat must be run from the project root folder.
    echo Current folder does not contain 'run.py'.
    echo.
    pause
    exit /b 1
)

if not exist "requirements.txt" (
    echo [ERROR] 'requirements.txt' was not found in the project root folder.
    echo Please make sure all repository files are present.
    echo.
    pause
    exit /b 1
)

rem 3. CRITICAL DATA CHECK: Verify existing .env exists
if not exist ".env" (
    echo ======================================================================
    echo [ERROR] Configuration file '.env' was not found in project root!
    echo ======================================================================
    echo This launcher runs your transferred original website and requires
    echo your original '.env' file.
    echo.
    echo Troubleshooting Steps:
    echo  1. Make sure your original '.env' was included in your transferred ZIP.
    echo  2. Copy your original '.env' into this project folder.
    echo.
    echo To protect your data, the launcher will NOT silently generate a new .env
    echo or create an empty demo database.
    echo.
    pause
    exit /b 1
)

rem 4. Check for compatible Python 3.10+ installation on this computer
echo [1/3] Checking Python installation...
set "PYTHON_EXE="

py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=py -3"
) else (
    python --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=python"
    )
)

if "%PYTHON_EXE%"=="" (
    echo.
    echo [ERROR] Python was not found on this computer.
    echo Python 3.10 or newer is required to run the Online Food Shop.
    echo.
    echo Troubleshooting Steps:
    echo  1. Download Python 3.10 or newer from: https://www.python.org/downloads/windows/
    echo  2. IMPORTANT: During installation, ensure the checkbox:
    echo        "Add python.exe to PATH"
    echo     is CHECKED before clicking "Install Now".
    echo  3. Restart your command prompt or double-click start_shop.bat again.
    echo.
    pause
    exit /b 1
)

rem Verify Python version (>= 3.10)
%PYTHON_EXE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python 3.10 or newer is required.
    for /f "tokens=*" %%v in ('%PYTHON_EXE% --version 2^>^&1') do echo Detected version: %%v
    echo Please install Python 3.10 or newer from https://www.python.org/
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('%PYTHON_EXE% --version 2^>^&1') do echo [OK] Found %%v (%PYTHON_EXE%)

rem 5. Virtual Environment Check & Relocation Detection
echo.
echo [2/3] Verifying isolated virtual environment (.venv)...
set "REBUILD_VENV=0"

if exist ".venv\Scripts\python.exe" (
    rem Test running inside .venv to verify it works on THIS machine
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0)" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] Existing virtual environment in '.venv' is invalid or was copied from another PC.
        echo Rebuilding isolated virtual environment for this computer...
        set "REBUILD_VENV=1"
    ) else (
        echo [OK] Virtual environment is valid for this computer.
    )
) else (
    echo [INFO] Virtual environment not found in '.venv'. Creating new virtual environment...
    set "REBUILD_VENV=1"
)

if "%REBUILD_VENV%"=="1" (
    if exist ".venv" (
        echo Removing stale virtual environment...
        rmdir /s /q ".venv" >nul 2>&1
    )
    echo Creating virtual environment in .venv ...
    %PYTHON_EXE% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to create virtual environment in .venv.
        echo Please verify you have read/write permissions in this folder.
        echo.
        pause
        exit /b 1
    )
    echo [OK] Created virtual environment in .venv.

    echo.
    echo Installing application dependencies from requirements.txt...
    echo (Note: Internet access is required during this initial setup)
    call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install project dependencies.
        echo.
        echo Troubleshooting Steps:
        echo  1. Check your internet connection (required for initial download).
        echo  2. If your school uses an HTTP/HTTPS proxy, configure pip proxy settings:
        echo        pip install --proxy http://proxy:port -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo [OK] Application dependencies installed successfully.
)

rem 6. Compile localization translation catalogs if present
echo.
echo [3/3] Verifying localization catalogs...
if exist "translations" (
    call ".venv\Scripts\python.exe" -m babel.messages.frontend compile -d translations >nul 2>&1
    if not errorlevel 1 (
        echo [OK] Translation catalogs verified.
    ) else (
        echo [INFO] Using existing compiled translation catalogs.
    )
)

rem 7. Execute application launcher (runs database preflight, port check, and auto-browser)
echo.
call ".venv\Scripts\python.exe" -m app.launcher
if errorlevel 1 (
    echo.
    echo [INFO] Application terminated with an error. Please review the message above.
    echo.
    pause
    exit /b 1
)

echo.
echo [INFO] Website server has stopped cleanly.
echo.
pause
