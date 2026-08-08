# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files

# This spec lives in packaging/, so every source path is resolved against the
# repository root rather than the spec's own directory.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

block_cipher = None

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'templates'), 'templates'),
        (os.path.join(ROOT, 'static'),    'static'),
        *collect_data_files('flask'),
        *collect_data_files('jinja2'),
    ],
    hiddenimports=[
        'i2c_backends.mock',
        'i2c_backends.ch341',
        'i2c_backends.ch347',
        'i2c_backends.ftdi_backend',
        'flask',
        'jinja2',
        'werkzeug',
        'click',
        # pyftdi is imported lazily by ftdi_backend, so PyInstaller cannot see
        # it; without this the packaged EXE has no working FTDI backend.
        'pyftdi',
        'pyftdi.ftdi',
        'pyftdi.i2c',
        'usb',
        'usb.backend.libusb1',
        'serial',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PIL'],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas,
    name='CMIS_Module_Manager',
    debug=False,
    strip=False,
    upx=False,
    console=True,
    onefile=True,
)
