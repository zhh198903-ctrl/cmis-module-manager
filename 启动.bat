@echo off
title CMIS Module Manager

REM Run from this script's own folder. A shortcut, a taskbar pin or a
REM terminal sitting somewhere else all start the batch file with a
REM different current directory, and app.py is then nowhere to be found.
cd /d "%~dp0"

REM Try py launcher first, then python, then python3
set PYTHON=
where py >nul 2>&1 && set PYTHON=py
if "%PYTHON%"=="" where python >nul 2>&1 && set PYTHON=python
if "%PYTHON%"=="" where python3 >nul 2>&1 && set PYTHON=python3

if "%PYTHON%"=="" (
    echo ERROR: Python not found. Please install Python from https://python.org
    pause
    exit /b 1
)

echo Python found: %PYTHON%
echo Checking dependencies...
%PYTHON% -c "import flask" >nul 2>&1
if not errorlevel 1 goto :run

echo Installing dependencies...
%PYTHON% -m pip install flask --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

:run
echo Starting CMIS Module Manager...
echo Open browser: http://127.0.0.1:5000
%PYTHON% app.py
set EXITCODE=%errorlevel%

echo.
if not "%EXITCODE%"=="0" (
    echo The server exited with code %EXITCODE%. The message above says why.
) else (
    echo Server stopped.
)
echo Press any key to close.
pause >nul
