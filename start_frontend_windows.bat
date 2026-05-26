@echo off
echo Bitte nutze ab jetzt START_APP.bat im Hauptordner.
echo Dieses alte Script startet nur das Frontend.
echo.
cd /d "%~dp0frontend"
npm install
npm run dev
pause
