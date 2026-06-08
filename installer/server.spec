---SERVER.SPEC---
# -*- mode: python ; coding: utf-8 -*-
block_cipher = None
 
a = Analysis(
    ['../server.py'],
    pathex=['..'],
    binaries=[],
    datas=[
        ('../static', 'static'),
        ('../lib', 'lib'),
        ('../.env.example', '.'),
    ],
    hiddenimports=[
        'msal', 'msal.application', 'msal.authority',
        'flask', 'flask_cors', 'flask_session',
        'PIL', 'PIL.Image', 'numpy',
        'win32serviceutil', 'win32service', 'win32event', 'servicemanager',
        'datasources.vistasoft_source', 'datasources.vistasoft_target',
        'datasources.dtxstudio_source', 'datasources.dtxstudio_target',
        'datasources.sopro_source', 'datasources.fb_client',
        'core.engine', 'core.migration_store', 'core.models',
        'core.base_datasource', 'auth.ms365',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'test', 'unittest'],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='ITInfinityServer',
    icon='../static/icon.ico',
    console=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
               name='ITInfinityServer')
---END SERVER.SPEC---
 
To use the spec instead of build.bat flags:
    pyinstaller installer/server.spec