@echo off
setlocal EnableDelayedExpansion

title Online Food Shop - Demo Server

echo ======================================================================
echo           Online Food Shop - School Demonstration Server
echo ======================================================================
echo.

rem 1. Navigate to script directory to ensure correct working directory
cd /d "%~dp0"

rem 2. Verify virtual environment exists
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment was not found - .venv\Scripts\python.exe is missing.
    echo.
    echo Please run 'setup_demo.bat' first to install the required dependencies
    echo and set up the local demonstration environment.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Activation script was not found - .venv\Scripts\activate.bat is missing.
    echo Please re-run 'setup_demo.bat' to recreate the virtual environment.
    echo.
    pause
    exit /b 1
)

rem 3. Verify application root
if not exist "run.py" (
    echo [ERROR] 'run.py' was not found in the current directory.
    echo Please execute 'start_demo.bat' from the project root directory.
    echo.
    pause
    exit /b 1
)

rem 4. Activate local virtual environment
echo Activating local virtual environment (.venv)...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    echo.
    pause
    exit /b 1
)

rem 5. Configure safe demonstration parameters (strictly local loopback 127.0.0.1)
set "HOST=127.0.0.1"
set "PORT=5000"

rem Explicitly disable debug mode for school presentation
rem Prevents interactive debugger pins and detailed stack traces from being exposed in public presentations
set "FLASK_DEBUG=0"

rem 6. Display clear local URL and status
echo.
echo ======================================================================
echo  Server Status:  ONLINE (Local Demonstration Mode)
echo  Local Address:  http://%HOST%:%PORT%
echo  Security:       Bound strictly to local loopback (127.0.0.1)
echo  Debug Mode:     Disabled (Safe for presentations)
echo ======================================================================
echo.
echo Open your web browser and navigate to:
echo.
echo        http://%HOST%:%PORT%
echo.
echo Simulated Email Output:
echo  - With default offline settings, account verification and password
echo    reset links will be printed directly in this terminal window below.
echo.
echo ======================================================================
echo  To STOP the demonstration server, press:   Ctrl + C
echo ======================================================================
echo.

rem 7. Launch the Flask application using the existing application factory
python -c "from app import create_app; app = create_app(); app.run(host='%HOST%', port=%PORT%, debug=False)"

rem If server exits, notify the presenter
echo.
echo [INFO] Demonstration server has stopped.
echo.
pause
