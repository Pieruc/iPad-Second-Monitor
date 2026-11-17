@echo off
rem Apre il pannello di controllo (senza finestra di console)
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" pythonw "%~dp0pannello.pyw"
