# -*- mode: python ; coding: utf-8 -*-
"""Onefile build of the CMIS Module Manager.

Everything else is found by PyInstaller's static analysis: app.py imports
Flask and the backend packages directly, and PyInstaller ships hooks for
Flask/Jinja2, so no hiddenimports or collect_data_files calls are needed.
"""
import os

# The spec lives in packaging/, so source paths resolve against the repo root.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, 'templates'), 'templates'),
        (os.path.join(ROOT, 'static'),    'static'),
    ],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PIL'],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name='CMIS_Module_Manager',
    console=True,
)
