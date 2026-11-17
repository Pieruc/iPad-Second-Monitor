@echo off
rem Riavvia il Virtual Display Driver per fargli rileggere vdd_settings.xml
rem (necessario dopo aver aggiunto nuove risoluzioni). Richiede i permessi di amministratore.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Richiedo i permessi di amministratore...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Riavvio del Virtual Display Driver...
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-PnpDevice -FriendlyName 'Virtual Display Driver').InstanceId"`) do (
    pnputil /restart-device "%%i"
)
echo.
echo Fatto. Ora puoi scegliere la nuova risoluzione.
pause
