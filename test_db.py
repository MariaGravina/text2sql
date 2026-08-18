import os
import oracledb

# --- CONFIGURAZIONE PARAMETRI ---
USER = "ADMIN"
PASSWORD = "Magrria1710?"  
DSN = "text2sqldb_high"            

# Percorso assoluto alla cartella del Wallet estratta
WALLET_DIR = os.path.abspath("Wallet_text2sqldb")

try:
    # Connessione ad Oracle Autonomous Database
    connection = oracledb.connect(
        user=USER,
        password=PASSWORD,
        dsn=DSN,
        config_dir=WALLET_DIR,         # Indica dove trovare tnsnames.ora / sqlnet.ora
        wallet_location=WALLET_DIR,    # Indica dove trovare i certificati
        wallet_password=PASSWORD
    )
    
    print("✅ Connessione ad Oracle Autonomous Database completata con successo!")
    
    cursor = connection.cursor()
    cursor.execute("SELECT 'Connessione OK!' FROM dual")
    risultato = cursor.fetchone()
    print("Risultato dal server Oracle:", risultato[0])
    
    cursor.close()
    connection.close()

except Exception as e:
    print("❌ Errore durante la connessione:", e)