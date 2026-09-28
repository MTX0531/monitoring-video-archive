@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   NVR Surveillance Video Archiver
echo ============================================================
echo   Working dir : %~dp0
echo   Stop        : press Ctrl+C
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    echo         Install Python 3.9+ or edit this file to use a full path.
    echo.
    pause
    exit /b 1
)

python "%~dp0nvr_puller.py" %*

echo.
echo Process finished with exit code %ERRORLEVEL%.
pause
