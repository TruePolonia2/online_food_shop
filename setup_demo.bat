@echo off
setlocal EnableDelayedExpansion

title Online Food Shop - Demo Setup

echo ======================================================================
echo           Online Food Shop - Windows Demonstration Setup
echo ======================================================================
echo.

rem 1. Navigate to script directory to ensure correct working directory
cd /d "%~dp0"

rem 2. Verify working directory contains project files
if not exist "run.py" (
    echo [ERROR] setup_demo.bat must be run from the project root folder.
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

rem 3. Check for compatible Python 3.10+ installation
echo [1/4] Checking Python installation...
set "PYTHON_EXE="

rem Test 'py -3' Windows launcher first, then 'python'
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
    echo [ERROR] Python 3 was not detected on this system.
    echo.
    echo Troubleshooting Steps:
    echo  1. Download Python 3.10 or newer from: https://www.python.org/downloads/windows/
    echo  2. IMPORTANT: During installation, ensure the checkbox:
    echo        "Add python.exe to PATH"
    echo     is CHECKED before clicking "Install Now".
    echo  3. Restart your command prompt or terminal window and run setup_demo.bat again.
    echo.
    pause
    exit /b 1
)

rem Validate Python version (must be >= 3.10)
%PYTHON_EXE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python 3.10 or newer is required to run the Online Food Shop.
    for /f "tokens=*" %%v in ('%PYTHON_EXE% --version 2^>^&1') do echo Detected version: %%v
    echo Please install Python 3.10 or newer from https://www.python.org/
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('%PYTHON_EXE% --version 2^>^&1') do echo [OK] Found %%v (%PYTHON_EXE%)

rem 4. Create local virtual environment (.venv) if missing
echo.
echo [2/4] Configuring isolated virtual environment (.venv)...
if not exist ".venv\Scripts\python.exe" (
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
) else (
    echo [OK] Virtual environment already exists in .venv.
)

rem 5. Install dependencies from requirements.txt
echo.
echo [3/4] Installing application dependencies from requirements.txt...
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install project dependencies.
    echo.
    echo Troubleshooting Steps:
    echo  1. Check your internet connection - required for initial package downloads.
    echo  2. If your school uses an HTTP/HTTPS proxy, configure pip proxy settings:
    echo        pip install --proxy http://proxy:port -r requirements.txt
    echo  3. Verify requirements.txt has not been corrupted or modified.
    echo.
    pause
    exit /b 1
)
echo [OK] Application dependencies successfully installed.

rem 6. Verify and compile localization translation catalogs
echo.
echo [4/4] Verifying and compiling localization catalogs (Babel)...
if exist "translations" (
    call ".venv\Scripts\python.exe" -m babel.messages.frontend compile -d translations >nul 2>&1
    if not errorlevel 1 (
        echo [OK] English and European Portuguese translation catalogs compiled.
    ) else (
        echo [INFO] Using existing compiled translation catalogs.
    )
)

rem 7. Safe local demo .env configuration
if not exist ".env" (
    echo.
    echo [INFO] No .env configuration detected.
    echo Generating isolated demo configuration with a secure random secret key...
    call ".venv\Scripts\python.exe" -c "import secrets; sk = secrets.token_hex(32); lines = ['# Online Food Shop - Local Demo Configuration', 'FLASK_CONFIG=development', 'FLASK_DEBUG=0', f'SECRET_KEY={sk}', 'DATABASE_PATH=instance/demo_food_shop.sqlite3', 'EMAIL_BACKEND=memory', 'EMAIL_DEV_CONSOLE_PREVIEW=True', 'APP_BASE_URL=http://127.0.0.1:5000', 'STRIPE_CURRENCY=eur', 'STRIPE_SECRET_KEY=sk_test_demo_placeholder', 'STRIPE_PUBLISHABLE_KEY=pk_test_demo_placeholder', 'STRIPE_WEBHOOK_SECRET=whsec_demo_placeholder']; open('.env', 'w', encoding='utf-8').write('\n'.join(lines) + '\n')"
    if exist ".env" (
        echo [OK] Created local demo .env (isolated database: instance/demo_food_shop.sqlite3).
    ) else (
        echo [WARNING] Could not auto-generate .env; copying .env.example fallback.
        if exist ".env.example" copy /y ".env.example" ".env" >nul
    )
) else (
    echo.
    echo [INFO] Existing .env configuration detected. Preserving without changes.
)

rem 8. Initialize database schema idempotently
echo.
echo Initializing database schema (idempotent, preserves existing data)...
call ".venv\Scripts\python.exe" -c "from app import create_app; from app.database.connection import init_db; app = create_app(); ctx = app.app_context(); ctx.push(); init_db()" >nul 2>&1
if not errorlevel 1 (
    echo [OK] SQLite database schema verified and ready.
) else (
    echo [WARNING] Could not initialize database schema automatically. It will initialize on startup.
)

echo.
echo ======================================================================
echo           Demo Setup Completed Successfully!
echo ======================================================================
echo.
echo Next Steps for Your School Presentation:
echo.
echo  1. Seed fictional demo catalog (categories and products):
echo        .venv\Scripts\python.exe -m flask --app run.py seed-demo
echo.
echo  2. (Optional) Create an Admin / Store Owner account:
echo        .venv\Scripts\python.exe -m flask --app run.py create-owner
echo.
echo  3. Start the demonstration server:
echo        Double-click 'start_demo.bat' or run 'start_demo.bat' in cmd.
echo.
echo  4. Open your browser to:
echo        http://127.0.0.1:5000
echo.
pause
