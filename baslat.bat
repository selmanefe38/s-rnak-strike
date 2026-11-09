@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Gerekli paketler kontrol ediliyor...
py -m pip install -r requirements.txt
echo.
echo Oyun baslatiliyor...
py main.py
pause