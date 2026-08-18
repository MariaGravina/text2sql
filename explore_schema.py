import os
import pandas as pd
from sqlalchemy import create_engine, inspect

# --- CONFIGURAZIONE ---
USER = "ADMIN"
PASSWORD = "Magrria1710?"  # Inserisci la tua password ADMIN
DSN = "text2sqldb_high"              # Il tuo DSN corretto

WALLET_DIR = os.path.abspath("Wallet_text2sqldb")
connection_string = f"oracle+oracledb://{USER}:{PASSWORD}@{DSN}?config_dir={WALLET_DIR}&wallet_location={WALLET_DIR}&wallet_password={PASSWORD}"

try:
    print("🚀 Connessione ad Oracle per l'esplorazione dello schema...\n")
    engine = create_engine(connection_string)
    inspector = inspect(engine)

    # Recupera tutte le tabelle presenti nello schema dell'utente
    table_names = inspector.get_table_names()
    print(f"📋 Trovate {len(table_names)} tabelle nello schema:\n")

    schema_summary = []

    for table in sorted(table_names):
        print(f"══════════════════════════════════════════════════")
        print(f"🔹 TABELLA: {table.upper()}")
        print(f"══════════════════════════════════════════════════")

        # Recupera colonne e tipi
        columns = inspector.get_columns(table)
        print("Colonne disponibili:")
        for col in columns:
            print(f"  • {col['name']:<30} | Tipo: {str(col['type']):<15} | Nullabile: {col['nullable']}")

        # Recupera Chiavi Primarie (se definite)
        pk_constraint = inspector.get_pk_constraint(table)
        pks = pk_constraint.get('constrained_columns', [])
        if pks:
            print(f"\n🔑 Chiave Primaria (PK): {', '.join(pks)}")

        # Recupera Chiavi Esterne / Relazioni (se definite)
        fks = inspector.get_foreign_keys(table)
        if fks:
            print("\n🔗 Chiavi Esterne (FK / Relazioni):")
            for fk in fks:
                print(f"  • {fk['constrained_columns']} ---> {fk['referred_table']}.{fk['referred_columns']}")

        print("\n")

except Exception as e:
    print("❌ Errore durante l'esplorazione dello schema:", e)