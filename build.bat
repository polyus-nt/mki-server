@echo off
setlocal

rem Always run relative to the directory containing this script.
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "APP_NAME=TJC-Commutator"
set "ENTRY_POINT=simplebus_serial.py"

if not exist "%ENTRY_POINT%" (
    echo ERROR: Entry point "%ENTRY_POINT%" was not found.
    goto :error
)

if not exist "%PYTHON%" (
    echo Creating virtual environment...
    py -3 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Python 3.10 or newer is required.
        goto :error
    )
)

echo Installing build dependencies...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :error

"%PYTHON%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo Building %APP_NAME%.exe...
"%PYTHON%" -m PyInstaller --noconfirm --clean --windowed --name "%APP_NAME%" --paths "." --collect-all matplotlib "%ENTRY_POINT%"
if errorlevel 1 goto :error

echo.
echo Build complete: "%CD%\dist\%APP_NAME%\%APP_NAME%.exe"
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
