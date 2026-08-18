@echo off
echo Avvio di Text2SQL Assistant e del Tunnel Pubblico...

:: 1. Avvia Streamlit in una nuova finestra
start "Streamlit App" cmd /k "venv\Scripts\activate && streamlit run app.py"

:: 2. Attendi 3 secondi per permettere a Streamlit di avviarsi
timeout /t 3 /nobreak > nul

:: 3. Avvia Localtunnel in automatico (-y) con sottodominio fisso
start "Localtunnel" cmd /k "npx -y localtunnel --port 8501 --subdomain tesi-text2sql-maria"

echo Operazione completata! Il tuo link sara: https://tesi-text2sql-maria.loca.lt