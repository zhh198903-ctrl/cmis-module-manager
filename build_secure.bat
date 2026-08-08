@echo off
title Build CMIS Module Manager EXE (Protected)
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ============================================================
echo  CMIS Module Manager -- Secure Build (PyArmor + PyInstaller)
echo ============================================================
echo.

:: ── Step 1: Install tools ──────────────────────────────────────
echo [1/5] Installing PyArmor and PyInstaller...
pip install pyinstaller pyarmor --quiet
if errorlevel 1 ( echo ERROR: pip install failed. & goto :fail )

:: ── Step 2: Obfuscate Python source ───────────────────────────
echo [2/5] Obfuscating Python source code...

if exist "obf" rmdir /s /q "obf"

:: Obfuscate top-level modules
pyarmor gen -O obf app.py cmis_registers.py i2c_interface.py
if errorlevel 1 ( echo ERROR: pyarmor obfuscation failed. & goto :fail )

:: Obfuscate i2c_backends package (recursive)
pyarmor gen -O obf --recursive i2c_backends
if errorlevel 1 ( echo ERROR: pyarmor package obfuscation failed. & goto :fail )

echo     Source obfuscated to: obf\

:: ── Step 3: Detect PyArmor runtime package name ───────────────
echo [3/5] Detecting PyArmor runtime package...

set "RT_PKG="
for /d %%d in ("obf\pyarmor_runtime_*") do set "RT_PKG=%%~nxd"
if "!RT_PKG!"=="" (
    echo ERROR: pyarmor_runtime package not found in obf\
    goto :fail
)
echo     Found runtime: !RT_PKG!

:: ── Step 4: Build EXE from obfuscated source ──────────────────
echo [4/5] Building protected executable...

pyinstaller ^
  --clean --noconfirm ^
  --onefile ^
  --name CMIS_Module_Manager ^
  --console ^
  --paths "obf" ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --add-data "obf\!RT_PKG!;!RT_PKG!" ^
  --hidden-import flask ^
  --hidden-import jinja2 ^
  --hidden-import werkzeug ^
  --hidden-import click ^
  --hidden-import cmis_registers ^
  --hidden-import i2c_interface ^
  --hidden-import i2c_backends ^
  --hidden-import i2c_backends.mock ^
  --hidden-import i2c_backends.ch341 ^
  --hidden-import i2c_backends.ch347 ^
  --hidden-import i2c_backends.ftdi_backend ^
  --exclude-module tkinter ^
  --exclude-module matplotlib ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  "obf\app.py"

if errorlevel 1 ( echo ERROR: PyInstaller build failed. & goto :fail )

:: ── Step 5: Copy to CMIS2Customer ─────────────────────────────
echo [5/5] Copying to CMIS2Customer...

if not exist "CMIS2Customer" mkdir "CMIS2Customer"
copy /y "dist\CMIS_Module_Manager.exe" "CMIS2Customer\"
if errorlevel 1 ( echo WARNING: Could not copy EXE ^(file in use?^) )

echo.
echo ============================================================
echo  BUILD COMPLETE
echo  Protected EXE: %~dp0CMIS2Customer\CMIS_Module_Manager.exe
echo  Source protection: PyArmor bytecode obfuscation
echo  NOTE: templates/ and static/ are served to browser
echo        and cannot be hidden by design.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ============================================================
echo  BUILD FAILED -- See error above
echo ============================================================
pause
exit /b 1
