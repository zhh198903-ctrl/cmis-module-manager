@echo off
title Build CMIS Module Manager EXE
cd /d "%~dp0"

echo [1/3] Installing PyInstaller...
pip install pyinstaller --quiet

echo [2/3] Building executable...
python -m PyInstaller CMIS.spec --clean --noconfirm

if errorlevel 1 (
    echo BUILD FAILED.
    pause
    exit /b 1
)

echo [3/3] Copying to CMIS2Customer...
if not exist "CMIS2Customer" mkdir "CMIS2Customer"
copy /y "dist\CMIS_Module_Manager.exe" "CMIS2Customer\"

echo.
echo Done! Executable is at:
echo   %~dp0CMIS2Customer\CMIS_Module_Manager.exe
pause
