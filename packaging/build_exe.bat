@echo off
title Build CMIS Module Manager EXE

REM Run from the repository root so the spec's paths resolve correctly.
cd /d "%~dp0.."

REM The shipped EXE must be 32-bit: the WCH CH341/CH347 driver installs a
REM 32-bit CH341DLL.dll, and a 64-bit process cannot load it.
REM
REM This used to fall back to whatever "python" was on PATH whenever
REM CMIS_PYTHON was unset, on the reasoning that a 64-bit build is fine for a
REM local test. It is - but the same command builds the release, and a 64-bit
REM build is indistinguishable from a correct one afterwards: it starts,
REM serves the interface, passes every mock-backed test, and reports the
REM CH341 backend as unavailable, which is also what an unplugged adapter
REM looks like. So the default is now the 32-bit interpreter, found through
REM the Windows launcher, and a 64-bit build has to be asked for by name.
if not defined CMIS_PYTHON (
    py -3-32 -c "import sys" >nul 2>&1 && set "CMIS_PYTHON=py -3-32"
)
if not defined CMIS_PYTHON set "CMIS_PYTHON=python"

echo Building CMIS_Module_Manager.exe with "%CMIS_PYTHON%" ...
%CMIS_PYTHON% -c "import struct,sys;print('  interpreter: %%d-bit Python %%s' %% (8*struct.calcsize('P'), sys.version.split()[0]))"

%CMIS_PYTHON% -c "import struct,sys;sys.exit(0 if struct.calcsize('P')==4 else 1)"
if errorlevel 1 (
    if not defined CMIS_ALLOW_64BIT (
        echo.
        echo REFUSING TO BUILD: that interpreter is 64-bit, so the exe cannot
        echo load the 32-bit CH341DLL.dll and the CH341/CH347 adapters would
        echo report themselves as not attached.
        echo.
        echo   Release build:  set CMIS_PYTHON=C:\path\to\32-bit\python.exe
        echo                   ^(or install a 32-bit Python for "py -3-32"^)
        echo   Local test:     set CMIS_ALLOW_64BIT=1
        echo.
        pause
        exit /b 1
    )
    echo   CMIS_ALLOW_64BIT is set - building a 64-bit exe for local testing.
    echo   packaging\make_dist_zip.py will refuse to package it.
)

%CMIS_PYTHON% -m PyInstaller "packaging\CMIS.spec" --clean --noconfirm ^
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
