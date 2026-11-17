@echo off
rem Avvia il secondo schermo via cavo USB (serve l'app "Secondo Schermo" sull'iPad)
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
python server.py --usb %*
pause
