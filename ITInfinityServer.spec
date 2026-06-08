# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('static', 'static'), ('lib', 'lib'), ('.env', '.')]
binaries = []
hiddenimports = ['msal', 'flask_cors', 'PIL', 'PIL.Image', 'numpy', 'win32serviceutil', 'win32service', 'win32event', 'servicemanager', 'datasources.vistasoft_source', 'datasources.vistasoft_target', 'datasources.dtxstudio_source', 'datasources.dtxstudio_target', 'datasources.sopro_source', 'datasources.fb_client', 'core.engine', 'core.migration_store', 'core.models', 'core.base_datasource', 'auth.ms365']
tmp_ret = collect_all('msal')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('flask')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'test', 'unittest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ITInfinityServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['static\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ITInfinityServer',
)
