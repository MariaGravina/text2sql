import os
import warnings
from urllib.parse import quote_plus
from dotenv import load_dotenv
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent
from langchain_google_genai import ChatGoogleGenerativeAI

warnings.filterwarnings("ignore")

# --- CONFIGURAZIONE ---
# Carica le variabili definite nel file .env (nella stessa cartella dello script)
load_dotenv()

REQUIRED_ENV_VARS = ["GOOGLE_API_KEY", "ORACLE_ADMIN_PASSWORD", "ORACLE_WALLET_PASSWORD"]
mancanti = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if mancanti:
    raise RuntimeError(
        f"Variabili mancanti nel file .env: {', '.join(mancanti)}\n"
        "Crea un file .env nella stessa cartella dello script con:\n"
        "  GOOGLE_API_KEY=la-tua-chiave\n"
        "  ORACLE_ADMIN_PASSWORD=la-password-admin\n"
        "  ORACLE_WALLET_PASSWORD=la-password-del-wallet"
    )

USER = "ADMIN"
PASSWORD = os.environ["ORACLE_ADMIN_PASSWORD"]
WALLET_PASSWORD = os.environ["ORACLE_WALLET_PASSWORD"]
DSN = "text2sqldb_high"

encoded_user = quote_plus(USER)
encoded_password = quote_plus(PASSWORD)
encoded_wallet_password = quote_plus(WALLET_PASSWORD)

WALLET_DIR = os.path.abspath("Wallet_text2sqldb")

connection_string = (
    f"oracle+oracledb://{encoded_user}:{encoded_password}@{DSN}"
    f"?config_dir={WALLET_DIR}&wallet_location={WALLET_DIR}&wallet_password={encoded_wallet_password}"
)

# Prompt guida per l'agente (nessun placeholder ReAct manuale:
# con il tool-calling di Gemini, LangChain gestisce tools/azioni da sé)
PREFIX_PROMPT = """Sei un agente SQL esperto per un database Oracle.
Rispondi sempre in italiano, in modo chiaro e comprensibile anche per un utente non tecnico.
Quando ti serve un dato, interroga il database usando gli strumenti disponibili.
Prima di eseguire una query su una tabella, verifica sempre la struttura delle tabelle rilevanti (colonne, tipi).
Se la domanda è ambigua, fai la scelta più ragionevole e spiega brevemente l'assunzione fatta nella risposta finale.
Se una query fallisce, correggila e riprova prima di arrenderti.
"""


def crea_agente_sql():
    print("🔌 Connessione al database Oracle e inizializzazione Gemini...")

    db = SQLDatabase.from_uri(
        connection_string,
        sample_rows_in_table_info=2
    )

    llm = ChatGoogleGenerativeAI(
        model="gemini-3.5-flash",
        temperature=0
    )

    agent = create_sql_agent(
        llm=llm,
        db=db,
        verbose=True,
        prefix=PREFIX_PROMPT,
        agent_type="tool-calling",
        handle_parsing_errors=True
    )

    return agent


def estrai_testo_risposta(output):
    """Gestisce sia risposte come stringa semplice sia come lista di blocchi
    strutturati (formato usato da alcuni modelli Gemini più recenti)."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parti = []
        for blocco in output:
            if isinstance(blocco, dict) and blocco.get("type") == "text":
                parti.append(blocco.get("text", ""))
        if parti:
            return "\n".join(parti)
    return str(output)


if __name__ == "__main__":
    try:
        agent = crea_agente_sql()
        print("✅ Agente Text-to-SQL con Gemini pronto!\n")

        while True:
            domanda = input("❓ Fai una domanda sul database (oppure scrivi 'esci'): ")
            if domanda.lower() in ["esci", "exit", "quit"]:
                break

            if domanda.strip():
                print("\n🧠 Generazione query ed esecuzione su Oracle...\n")
                try:
                    risposta = agent.invoke({"input": domanda})
                    testo = estrai_testo_risposta(risposta["output"])
                    print(f"\n💡 Risposta: {testo}\n")
                except Exception as e:
                    print(f"\n⚠️ Errore durante l'esecuzione della domanda: {e}\n")
                print("-" * 50)

    except Exception as e:
        print("❌ Errore durante l'inizializzazione:", e)