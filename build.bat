@echo off
setlocal

echo [1/4] Installing build tools...
python -m pip install pyinstaller pywin32 --quiet

echo [2/4] Building server executable...
python -m PyInstaller ^
  --noconfirm ^
  --onedir ^
  --name ITInfinityServer ^
  --icon static\icon.ico ^
  --add-data "static;static" ^
  --add-data "lib;lib" ^
  --add-data ".env;." ^
  --hidden-import msal ^
  --hidden-import flask_cors ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import numpy ^
  --hidden-import datasources.vistasoft_source ^
  --hidden-import datasources.vistasoft_target ^
  --hidden-import datasources.dtxstudio_source ^
  --hidden-import datasources.dtxstudio_target ^
  --hidden-import datasources.sopro_source ^
  --hidden-import datasources.fb_client ^
  --hidden-import core.engine ^
  --hidden-import core.migration_store ^
  --hidden-import core.models ^
  --hidden-import core.base_datasource ^
  --hidden-import auth.ms365 ^
  --collect-all msal ^
  --collect-all flask ^
  --exclude-module tkinter ^
  --exclude-module test ^
  --exclude-module unittest ^
  server.py

if %ERRORLEVEL% neq 0 (
    echo ERROR: Server build failed.
    pause
    exit /b 1
)

echo [3/4] Building service launcher...
python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --name ITInfinityService ^
  --icon static\icon.ico ^
  --hidden-import win32serviceutil ^
  --hidden-import win32service ^
  --hidden-import win32event ^
  --hidden-import servicemanager ^
  service_launcher.py

if %ERRORLEVEL% neq 0 (
    echo ERROR: Service launcher build failed.
    pause
    exit /b 1
)

echo [4/4] Building installer...
makensis installer\setup.nsi

if %ERRORLEVEL% neq 0 (
    echo ERROR: NSIS build failed.
    echo Download NSIS from: https://nsis.sourceforge.io/Download
    pause
    exit /b 1
)

echo.
echo Done. Installer is at installer\IT-INFINITY-Migration-Tool-Setup.exe
pause