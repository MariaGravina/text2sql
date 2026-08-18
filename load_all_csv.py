import os
import glob
import pandas as pd
from sqlalchemy import create_engine, Float, Numeric
from sqlalchemy.dialects import oracle

# --- CONFIGURAZIONE ---
USER = "ADMIN"
PASSWORD = "Magrria1710?"  
DSN = "text2sqldb_high"             

# Cartella contenente i 13 file CSV
CSV_FOLDER = "dataset_csv"

# Percorso assoluto del Wallet
WALLET_DIR = os.path.abspath("Wallet_text2sqldb")

# Stringa di connessione per Oracle via SQLAlchemy
connection_string = f"oracle+oracledb://{USER}:{PASSWORD}@{DSN}?config_dir={WALLET_DIR}&wallet_location={WALLET_DIR}&wallet_password={PASSWORD}"

try:
    print("🚀 Connessione al Database Oracle in corso...")
    engine = create_engine(connection_string)

    csv_files = glob.glob(os.path.join(CSV_FOLDER, "*.csv"))

    if not csv_files:
        print(f"⚠️ Nessun file CSV trovato nella cartella '{CSV_FOLDER}'!")
    else:
        print(f"📁 Trovati {len(csv_files)} file CSV da caricare.\n")

        # Definizione del tipo FLOAT compatibile sia con SQLAlchemy generale che con Oracle
        oracle_float = Float(precision=53).with_variant(oracle.FLOAT(binary_precision=126), 'oracle')

        for file_path in csv_files:
            file_name = os.path.basename(file_path)
            table_name = os.path.splitext(file_name)[0].strip().replace(" ", "_").upper()
            
            print(f"⏳ Caricamento di '{file_name}' nella tabella '{table_name}'...")

            # Lettura del file CSV
            try:
                df = pd.read_csv(file_path, sep=None, engine='python')
            except Exception:
                df = pd.read_csv(file_path, sep=';')

            # Normalizzazione nomi colonne
            df.columns = [str(col).strip().replace(" ", "_").replace("-", "_").upper() for col in df.columns]

            # Mappatura dei tipi di dati per i valori decimali
            dtype_mapping = {}
            for col, dtype in df.dtypes.items():
                if "float" in str(dtype):
                    dtype_mapping[col] = oracle_float

            # Caricamento del DataFrame su Oracle
            df.to_sql(
                table_name.lower(), 
                con=engine, 
                if_exists='replace', 
                index=False,
                dtype=dtype_mapping
            )
            print(f"   ✅ Tabella '{table_name}' creata con successo ({len(df)} righe)!\n")

        print("🎉 Caricamento di tutti i file CSV completato con successo!")

except Exception as e:
    print("❌ Si è verificato un errore durante il caricamento massivo:", e)