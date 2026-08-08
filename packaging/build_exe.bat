@echo off
title Build CMIS Module Manager EXE

REM Run from the repository root so the spec's paths resolve correctly.
cd /d "%~dp0.."

echo Building CMIS_Module_Manager.exe ...
python -m PyInstaller "packaging\CMIS.spec" --clean --noconfirm ^
  --distpath "CMIS2Customer" --workpath "build"

if errorlevel 1 (
    echo BUILD FAILED.
    pause
    exit /b 1
)

rmdir /s /q "build" 2>nul

echo.
echo Done: %~dp0..\CMIS2Customer\CMIS_Module_Manager.exe
pause
