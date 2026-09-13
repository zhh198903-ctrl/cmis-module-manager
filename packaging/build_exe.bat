@echo off
title Build CMIS Module Manager EXE

REM Run from the repository root so the spec's paths resolve correctly.
cd /d "%~dp0.."

REM The shipped EXE must be 32-bit: the WCH CH341/CH347 driver installs a
REM 32-bit CH341DLL.dll, and a 64-bit process cannot load it. Set CMIS_PYTHON
REM to a 32-bit interpreter to build the release; without it the build uses
REM whatever "python" is on PATH, which is fine for a local test build.
if not defined CMIS_PYTHON set "CMIS_PYTHON=python"

echo Building CMIS_Module_Manager.exe with "%CMIS_PYTHON%" ...
"%CMIS_PYTHON%" -c "import struct,sys;print('  interpreter: %d-bit Python %s' % (8*struct.calcsize('P'), sys.version.split()[0]))"
"%CMIS_PYTHON%" -m PyInstaller "packaging\CMIS.spec" --clean --noconfirm ^
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
