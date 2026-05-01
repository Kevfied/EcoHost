@echo off
title MC-EcoHost Server Management System

echo ========================================
echo MC-EcoHost Server Management System
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ and add it to your PATH
    pause
    exit /b 1
)

REM Check if we're in the correct directory
if not exist "main.py" (
    echo ERROR: main.py not found in current directory
    echo Please run this batch file from the MC-EcoHost directory
    pause
    exit /b 1
)

REM Check if virtual environment exists
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
)

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    pause
    exit /b 1
)

REM Install/update dependencies
echo Installing/updating dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

echo.
echo Starting MC-EcoHost...
echo The web interface will be available at: http://localhost:8000
echo Press Ctrl+C to stop the server
echo.

REM Start the application
python main.py

REM Deactivate virtual environment on exit
call venv\Scripts\deactivate.bat

echo.
echo MC-EcoHost has been stopped.
pause
