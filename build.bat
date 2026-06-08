---BUILD.BAT---
@echo off
setlocal
 
echo [1/4] Installing build tools...
pip install pyinstaller --quiet
 
echo [2/4] Building server executable...
pyinstaller ^
  --noconfirm ^
  --onedir ^
  --name ITInfinityServer ^
  --icon static\icon.ico ^
  --add-data "static;static" ^
  --add-data "lib;lib" ^
  --add-data ".env.example;." ^
  --hidden-import msal ^
  --hidden-import flask_cors ^
  --hidden-import engineio ^
  --hidden-import PIL ^
  --hidden-import numpy ^
  --hidden-import win32serviceutil ^
  --hidden-import win32service ^
  --hidden-import win32event ^
  --hidden-import servicemanager ^
  --collect-all msal ^
  --collect-all flask ^
  server.py
 
echo [3/4] Building service launcher...
pyinstaller ^
  --noconfirm ^
  --onefile ^
  --name ITInfinityService ^
  --icon static\icon.ico ^
  --hidden-import win32serviceutil ^
  --hidden-import win32service ^
  --hidden-import win32event ^
  --hidden-import servicemanager ^
  service_launcher.py
 
echo [4/4] Building installer...
cd installer
makensis setup.nsi
cd ..
 
echo Done. Installer is at installer\IT-INFINITY-Migration-Tool-Setup.exe
---END BUILD.BAT---
