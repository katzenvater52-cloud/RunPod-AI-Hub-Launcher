@echo off
echo Bitte nutze ab jetzt START_APP.bat im Hauptordner.
echo Dieses alte Script startet nur das Backend.
echo.
cd /d "%~dp0backend"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000
pause
