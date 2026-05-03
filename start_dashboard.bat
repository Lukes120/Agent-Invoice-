@echo off
REM Launcher Windows per Dashboard Odoo Invoice Agent
REM Doppio click su questo file per avviare la webapp

cd /d "%~dp0"
echo.
echo ================================================
echo  Odoo Invoice Agent - Dashboard
echo ================================================
echo.
echo  Avvio webapp locale...
echo  Poi apri il browser su http://localhost:5000
echo.
echo  Premi CTRL+C per arrestare
echo ================================================
echo.

python webapp\app.py

pause
