@echo off
echo ============================================================
echo  Deploy file aggiornati sul server
echo ============================================================
echo.

set SRC=C:\Users\lranalletta\Documents\AGENT FATTURAZIONE PASSIVA\odoo_invoice_agent
set DST=C:\odoo_apps\invoice_agent

echo Copia config\rules.py ...
copy /Y "%SRC%\config\rules.py" "%DST%\config\rules.py"

echo Copia core\odoo_writer.py ...
copy /Y "%SRC%\core\odoo_writer.py" "%DST%\core\odoo_writer.py"

echo Copia core\fatturapa_analyzer.py ...
copy /Y "%SRC%\core\fatturapa_analyzer.py" "%DST%\core\fatturapa_analyzer.py"

echo Copia webapp\app.py ...
copy /Y "%SRC%\webapp\app.py" "%DST%\webapp\app.py"

echo Copia webapp\templates\invoices.html ...
copy /Y "%SRC%\webapp\templates\invoices.html" "%DST%\webapp\templates\invoices.html"

echo Copia CLAUDE.md ...
copy /Y "%SRC%\CLAUDE.md" "%DST%\CLAUDE.md"

echo.
echo ============================================================
echo  Deploy completato! Riavvia la webapp sul server.
echo ============================================================
pause
