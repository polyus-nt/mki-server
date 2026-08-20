@echo off
setlocal
cd /d "%~dp0"

cargo build --release
if errorlevel 1 goto :error

echo.
echo Build complete: "%CD%\target\release\tjc-commutator.exe"
exit /b 0

:error
echo.
echo Build failed. Install Rust from https://rustup.rs/ and run this script again.
pause
exit /b 1
