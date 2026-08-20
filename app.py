"""
Text2SQL Assistant — Streamlit, file unico.

Richiede:
  - In locale: file .env con GOOGLE_API_KEY, ORACLE_ADMIN_PASSWORD, ORACLE_WALLET_PASSWORD
  - Su Streamlit Cloud: Secrets configurati con le stesse chiavi + ORACLE_WALLET_BASE64

Avvio locale: streamlit run app.py
"""

import os
import re
import json
import uuid
import base64
import zipfile
import warnings
import time
from collections import deque
from dotenv import load_dotenv

load_dotenv()

os.environ["PYORACLEDB_THIN_MODE"] = "1"

import streamlit as st
import oracledb

oracledb.init_oracle_client = None

from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent
from langchain_google_genai import ChatGoogleGenerativeAI

warnings.filterwarnings("ignore")

WALLET_DIR_NAME = "Wallet_text2sqldb"

if not os.path.exists(WALLET_DIR_NAME):
    wallet_b64 = st.secrets.get("ORACLE_WALLET_BASE64") or os.environ.get("ORACLE_WALLET_BASE64")
    if wallet_b64:
        os.makedirs(WALLET_DIR_NAME, exist_ok=True)
        zip_path = "temp_wallet.zip"
        with open(zip_path, "wb") as f:
            f.write(base64.b64decode(wallet_b64))
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(WALLET_DIR_NAME)
        os.remove(zip_path)

WALLET_DIR = os.path.abspath(WALLET_DIR_NAME)


def apri_porta_firewall(porta: int = 8501):
    import platform
    import subprocess

    if platform.system() != "Windows":
        return

    nome_regola = "Streamlit App"
    try:
        check = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={nome_regola}"],
            capture_output=True, text=True, timeout=5,
        )
        if "No rules match" in check.stdout or check.returncode != 0:
            subprocess.run(
                [
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={nome_regola}",
                    "dir=in", "action=allow", "protocol=TCP",
                    f"localport={porta}",
                ],
                capture_output=True, text=True, timeout=5, check=True,
            )
            print(f"Regola firewall creata automaticamente per la porta {porta}.")
        else:
            print(f"Regola firewall per la porta {porta} già presente.")
    except Exception:
        print(f"Non sono riuscito ad aprire automaticamente la porta {porta} nel firewall.\n")


apri_porta_firewall()


def get_secret(key: str) -> str:
    if key in st.secrets:
        return st.secrets[key]
    return os.environ.get(key, "")


GOOGLE_API_KEY = get_secret("GOOGLE_API_KEY")
ORACLE_ADMIN_PASSWORD = get_secret("ORACLE_ADMIN_PASSWORD")
ORACLE_WALLET_PASSWORD = get_secret("ORACLE_WALLET_PASSWORD")

if GOOGLE_API_KEY:
    os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY

_mancanti = []
if not GOOGLE_API_KEY:
    _mancanti.append("GOOGLE_API_KEY")
if not ORACLE_ADMIN_PASSWORD:
    _mancanti.append("ORACLE_ADMIN_PASSWORD")
if not ORACLE_WALLET_PASSWORD:
    _mancanti.append("ORACLE_WALLET_PASSWORD")

if _mancanti:
    st.error(f"Variabili mancanti nel file .env o nei Secrets: {', '.join(_mancanti)}")
    st.stop()

DB_USER = "ADMIN"
DB_PASSWORD = ORACLE_ADMIN_PASSWORD
WALLET_PASSWORD = ORACLE_WALLET_PASSWORD
DSN = "text2sqldb_high"

MODEL_SIMPLE = "gemini-2.5-flash-lite"
MODEL_COMPLEX = "gemini-2.5-flash"


_allowed_tables_cache = None
_columns_cache = {}


def get_connection():
    return oracledb.connect(
        user=DB_USER,
        password=DB_PASSWORD,
        dsn=DSN,
        config_dir=WALLET_DIR,
        wallet_location=WALLET_DIR,
        wallet_password=WALLET_PASSWORD,
    )


def get_allowed_tables(force_refresh: bool = False):
    global _allowed_tables_cache
    if _allowed_tables_cache is not None and not force_refresh:
        return _allowed_tables_cache
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM user_tables ORDER BY table_name")
        _allowed_tables_cache = [row[0] for row in cur.fetchall()]
        return _allowed_tables_cache
    finally:
        conn.close()


def get_table_columns(table_name: str):
    table_name = table_name.upper().strip()
    if table_name not in [t.upper() for t in get_allowed_tables()]:
        raise ValueError(f"Tabella non riconosciuta nel database: {table_name}")
    if table_name in _columns_cache:
        return _columns_cache[table_name]

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT column_name, nullable, data_type
            FROM user_tab_columns
            WHERE table_name = :t
            ORDER BY column_id
            """,
            t=table_name,
        )
        cols = [{"name": r[0], "nullable": (r[1] == "Y"), "type": r[2]} for r in cur.fetchall()]
        _columns_cache[table_name] = cols
        return cols
    finally:
        conn.close()


def execute_insert(table_name: str, fields: dict):
    table_name = table_name.upper().strip()
    columns_info = get_table_columns(table_name)
    valid_columns = {c["name"] for c in columns_info}

    clean_fields = {}
    for raw_name, raw_value in fields.items():
        name = raw_name.upper().strip()
        if name not in valid_columns:
            raise ValueError(f"Colonna non valida per la tabella {table_name}: {raw_name}")
        value = None if raw_value in (None, "", "NULL", "null") else raw_value
        clean_fields[name] = value

    if not clean_fields:
        raise ValueError("Nessun campo valido da inserire.")

    col_names = list(clean_fields.keys())
    placeholders = [f":{i + 1}" for i in range(len(col_names))]
    sql = f"INSERT INTO {table_name} ({', '.join(col_names)}) VALUES ({', '.join(placeholders)})"
    values = [clean_fields[c] for c in col_names]

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, values)
        conn.commit()
        return sql
    finally:
        conn.close()


def execute_update(table_name: str, set_fields: dict, where_fields: dict):
    table_name = table_name.upper().strip()
    columns_info = get_table_columns(table_name)
    valid_columns = {c["name"] for c in columns_info}

    def _clean(fields):
        out = {}
        for raw_name, raw_value in fields.items():
            name = raw_name.upper().strip()
            if name not in valid_columns:
                raise ValueError(f"Colonna non valida per la tabella {table_name}: {raw_name}")
            value = None if raw_value in (None, "", "NULL", "null") else raw_value
            out[name] = value
        return out

    clean_set = _clean(set_fields)
    clean_where = _clean(where_fields)

    if not clean_set:
        raise ValueError("Nessun campo da modificare specificato.")
    if not clean_where:
        raise ValueError("Nessuna condizione WHERE specificata: un UPDATE senza filtro non è permesso.")

    set_cols = list(clean_set.keys())
    where_cols = list(clean_where.keys())

    set_clause = ", ".join(f"{c} = :{i + 1}" for i, c in enumerate(set_cols))
    where_clause = " AND ".join(
        f"{c} IS NULL" if clean_where[c] is None else f"{c} = :{len(set_cols) + i + 1}"
        for i, c in enumerate(where_cols)
    )
    sql = f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}"

    values = [clean_set[c] for c in set_cols] + [clean_where[c] for c in where_cols if clean_where[c] is not None]

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, values)
        righe = cur.rowcount
        conn.commit()
        return sql, righe
    finally:
        conn.close()


def fetch_matching_rows(table_name: str, where_fields: dict, limit: int = 10):
    """Recupera le righe reali che corrispondono alla condizione, per mostrarle come anteprima prima di modificare/eliminare."""
    table_name = table_name.upper().strip()
    columns_info = get_table_columns(table_name)
    valid_columns = {c["name"] for c in columns_info}

    clean_where = {}
    for raw_name, raw_value in where_fields.items():
        name = raw_name.upper().strip()
        if name not in valid_columns:
            raise ValueError(f"Colonna non valida per la tabella {table_name}: {raw_name}")
        clean_where[name] = None if raw_value in (None, "", "NULL", "null") else raw_value

    if not clean_where:
        raise ValueError("Nessuna condizione specificata.")

    where_cols = list(clean_where.keys())
    where_clause = " AND ".join(
        f"{c} IS NULL" if clean_where[c] is None else f"{c} = :{i + 1}"
        for i, c in enumerate(where_cols)
    )
    values = [clean_where[c] for c in where_cols if clean_where[c] is not None]

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM {table_name} WHERE {where_clause} FETCH FIRST {int(limit)} ROWS ONLY",
            values,
        )
        colonne = [d[0] for d in cur.description]
        righe = cur.fetchall()
        return colonne, righe
    finally:
        conn.close()


def count_matching(table_name: str, where_fields: dict) -> int:
    table_name = table_name.upper().strip()
    columns_info = get_table_columns(table_name)
    valid_columns = {c["name"] for c in columns_info}

    clean_where = {}
    for raw_name, raw_value in where_fields.items():
        name = raw_name.upper().strip()
        if name not in valid_columns:
            raise ValueError(f"Colonna non valida per la tabella {table_name}: {raw_name}")
        clean_where[name] = None if raw_value in (None, "", "NULL", "null") else raw_value

    if not clean_where:
        raise ValueError("Nessuna condizione specificata.")

    where_cols = list(clean_where.keys())
    where_clause = " AND ".join(
        f"{c} IS NULL" if clean_where[c] is None else f"{c} = :{i + 1}"
        for i, c in enumerate(where_cols)
    )
    values = [clean_where[c] for c in where_cols if clean_where[c] is not None]

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {where_clause}", values)
        return cur.fetchone()[0]
    finally:
        conn.close()


def execute_delete(table_name: str, where_fields: dict):
    table_name = table_name.upper().strip()
    columns_info = get_table_columns(table_name)
    valid_columns = {c["name"] for c in columns_info}

    clean_where = {}
    for raw_name, raw_value in where_fields.items():
        name = raw_name.upper().strip()
        if name not in valid_columns:
            raise ValueError(f"Colonna non valida per la tabella {table_name}: {raw_name}")
        clean_where[name] = None if raw_value in (None, "", "NULL", "null") else raw_value

    if not clean_where:
        raise ValueError("Nessuna condizione WHERE specificata: un DELETE senza filtro non è permesso.")

    where_cols = list(clean_where.keys())
    where_clause = " AND ".join(
        f"{c} IS NULL" if clean_where[c] is None else f"{c} = :{i + 1}"
        for i, c in enumerate(where_cols)
    )
    values = [clean_where[c] for c in where_cols if clean_where[c] is not None]
    sql = f"DELETE FROM {table_name} WHERE {where_clause}"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, values)
        righe = cur.rowcount
        conn.commit()
        return sql, righe
    finally:
        conn.close()


_PAROLE_VIETATE_GRAFICO = ("insert", "update", "delete", "drop", "alter", "truncate", "merge", "grant", "revoke", "create", ";")


def genera_query_grafico(domanda: str, tabelle_disponibili: list) -> str:
    """Chiede al modello una SINGOLA query SELECT di sola lettura, adatta per un grafico (2 colonne: etichetta + valore)."""
    elenco = ", ".join(tabelle_disponibili)
    prompt = f"""Sei un assistente Oracle SQL. L'utente vuole un grafico basato su questa richiesta:
"{domanda}"

Tabelle disponibili: {elenco}

Scrivi UNA SOLA query SQL Oracle di sola lettura (SELECT), che restituisca al massimo 2-3 colonne
adatte per un grafico: la prima colonna come etichetta/categoria (es. anno, mese, sesso, reparto),
le successive come valori numerici (es. conteggio, media). Usa GROUP BY e aggregazioni se serve.
Limita il risultato a un massimo di 50 righe con FETCH FIRST 50 ROWS ONLY.

Rispondi SOLO con la query SQL, senza markdown, senza spiegazioni, senza punto e virgola finale."""

    risposta = _llm().invoke(prompt)
    query = _testo(risposta.content).strip()
    query = re.sub(r"^```(sql)?|```$", "", query, flags=re.MULTILINE).strip().rstrip(";")
    return query


def esegui_query_sicura_sola_lettura(query: str):
    """Esegue una query SOLO se è una singola SELECT, senza parole chiave di scrittura/DDL. Solleva errore altrimenti."""
    q_lower = query.strip().lower()
    if not q_lower.startswith("select"):
        raise ValueError("Solo query di sola lettura (SELECT) sono permesse per i grafici.")
    for parola in _PAROLE_VIETATE_GRAFICO:
        if parola in q_lower:
            raise ValueError(f"Query non permessa: contiene '{parola}'.")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(query)
        colonne = [d[0] for d in cur.description]
        righe = cur.fetchall()
        return colonne, righe
    finally:
        conn.close()


def _llm(model=None):
    return ChatGoogleGenerativeAI(model=model or MODEL_SIMPLE, temperature=0)


def _testo(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parti = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parti)
    return str(content)


def analizza_richiesta(domanda: str, tabelle_disponibili: list) -> dict:
    elenco = ", ".join(tabelle_disponibili)
    prompt = f"""Sei un assistente che analizza una richiesta in linguaggio naturale su un database
Oracle di dati clinici cardiologici.

Tabelle disponibili: {elenco}

Richiesta dell'utente: "{domanda}"

Rispondi SOLO con un oggetto JSON valido (nessun testo prima o dopo, nessun blocco markdown),
in questo formato esatto:
{{
  "intento": "read" | "insert" | "update" | "delete" | "grafico" | "altro",
  "tabella": "NOME_TABELLA_O_NULL",
  "campi": {{"NOME_COLONNA": "valore", ...}},
  "where": {{"NOME_COLONNA": "valore", ...}},
  "solo_domanda": true | false
}}

Regole:
- "read": l'utente vuole solo interrogare/leggere dati (ricerche, conteggi, join, statistiche, analisi).
- "insert": l'utente vuole aggiungere una nuova riga. Usa "campi" per i valori forniti, "where" vuoto.
- "update": l'utente vuole modificare righe esistenti. Usa "campi" per i nuovi valori, "where" per identificare le righe.
- "delete": l'utente vuole eliminare righe esistenti. Usa "where" per identificare le righe, "campi" vuoto.
- "grafico": l'utente vuole vedere un grafico/visualizzazione dei dati (es. "mostrami un grafico di...", "andamento nel tempo di...", "distribuzione di...").
- "altro": la richiesta NON riguarda questo database (saluti generici, chiacchiere, argomenti non pertinenti).
- Per "read" e "grafico" imposta comunque "tabella" a null e lascia "campi"/"where" vuoti.
- "solo_domanda": metti true SOLO se per insert/update/delete l'utente sta chiedendo informazioni
  (es. "quali dati servono per inserire un paziente?", "cosa mi serve per modificare un esame?"),
  SENZA fornire alcun dato reale da inserire/modificare/eliminare. Metti false se l'utente sta
  effettivamente dando un comando con almeno un dato concreto (anche parziale).
- Non inventare valori non menzionati dall'utente."""

    risposta = _llm().invoke(prompt)
    testo = _testo(risposta.content).strip()
    testo = re.sub(r"^```(json)?|```$", "", testo, flags=re.MULTILINE).strip()
    try:
        dati = json.loads(testo)
    except json.JSONDecodeError:
        dati = {}

    intento = str(dati.get("intento", "read")).strip().lower()
    if intento not in ("read", "insert", "update", "delete", "grafico", "altro"):
        intento = "read"

    return {
        "intento": intento,
        "tabella": dati.get("tabella") or None,
        "campi": dati.get("campi", {}) or {},
        "where": dati.get("where", {}) or {},
        "solo_domanda": bool(dati.get("solo_domanda", False)),
    }


def descrivi_campi_tabella(tabella: str, colonne: list) -> str:
    """Genera una descrizione testuale dei campi di una tabella, per rispondere a 'quali dati servono?'."""
    righe = []
    for c in colonne:
        obbligatorio = "obbligatorio" if not c["nullable"] else "facoltativo"
        righe.append(f"- **{c['name'].lower()}** ({c['type'].lower()}, {obbligatorio})")
    return f"Per un'operazione sulla tabella **{tabella}**, questi sono i campi disponibili:\n\n" + "\n".join(righe) + \
        "\n\nDimmi i valori che vuoi usare e preparo l'operazione con conferma prima di eseguirla."


def genera_risposta_generica(domanda: str) -> str:
    """Genera una risposta breve e cordiale per richieste non pertinenti al database, invece di un errore o un messaggio fisso."""
    prompt = f"""Sei l'assistente di un'applicazione per interrogare e gestire un database Oracle
di dati clinici cardiologici (pazienti, esami, visite, coronarografie, ecc.).

L'utente ha scritto questo messaggio, che non sembra riguardare direttamente il database: "{domanda}"

Rispondi in modo breve, naturale e cordiale in italiano (massimo 2-3 frasi). Se il messaggio è un
saluto o small talk, rispondi in modo amichevole. Se è una domanda generica a cui puoi rispondere
con la tua conoscenza generale, dai una risposta utile e concisa. In ogni caso, ricorda con naturalezza
(senza essere ripetitivo o formale) che puoi anche aiutare con ricerche, statistiche o modifiche
sul database clinico, nel caso l'utente fosse interessato."""

    risposta = _llm().invoke(prompt)
    return _testo(risposta.content).strip()


PREFIX_PROMPT = """Sei un assistente esperto di Oracle SQL per un database clinico cardiologico.
Rispondi sempre in italiano.
Il tuo compito è trasformare una richiesta in linguaggio naturale in UNA SOLA query Oracle di sola lettura.
Sono consentite esclusivamente SELECT, JOIN, aggregazioni, filtri, CASE, sottoquery e CTE.
NON usare INSERT, UPDATE, DELETE, MERGE, DROP, ALTER, TRUNCATE, GRANT, REVOKE o CREATE.
Non modificare mai il database.
Usa esclusivamente le tabelle e le colonne presenti nello schema fornito.
Non inventare nomi di tabelle o colonne.
Se la richiesta non è sufficientemente determinata, genera comunque la query più ragionevole possibile.
La risposta deve essere SOLO JSON valido nel formato:
{"sql":"SELECT ...","spiegazione":"breve spiegazione in italiano"}.
"""

@st.cache_resource
def get_read_llm():
    return ChatGoogleGenerativeAI(model=MODEL_SIMPLE, temperature=0)

@st.cache_resource
def get_complex_llm():
    return ChatGoogleGenerativeAI(model=MODEL_COMPLEX, temperature=0)

@st.cache_data(ttl=300, show_spinner=False)
def get_schema_compact():
    tables = get_allowed_tables()
    schema = []
    for table in tables:
        cols = get_table_columns(table)
        schema.append({
            "table": table,
            "columns": [
                {"name": c["name"], "type": c["type"], "nullable": c["nullable"]}
                for c in cols
            ]
        })
    return schema

def _schema_text():
    return json.dumps(get_schema_compact(), ensure_ascii=False, separators=(",", ":"))

def _is_simple_message(message: str) -> bool:
    msg = re.sub(r"[!?.,;:]+", "", message.lower().strip())
    simple = {
        "ciao", "salve", "buongiorno", "buonasera", "buonanotte",
        "hey", "ok", "okay", "grazie", "grazie mille"
    }
    return msg in simple

def risposta_semplice(message: str):
    msg = message.lower().strip()
    if msg in {"grazie", "grazie mille"}:
        return "Di nulla! Se vuoi, posso aiutarti a interrogare il database."
    return "Ciao! Posso aiutarti a interrogare e analizzare il database clinico. Cosa vuoi sapere?"

def genera_sql_lettura(domanda: str):
    schema = _schema_text()
    prompt = f"""{PREFIX_PROMPT}

SCHEMA DEL DATABASE:
{schema}

RICHIESTA DELL'UTENTE:
{domanda}
"""
    risposta = get_read_llm().invoke(prompt)
    raw = _testo(risposta.content).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: estrai la prima SELECT dal testo.
        match = re.search(r"(?is)\bSELECT\b.*", raw)
        if not match:
            raise ValueError("Il modello non ha restituito una query SQL valida.")
        sql = match.group(0).strip().rstrip(";")
        return sql, ""

    sql = str(data.get("sql", "")).strip().rstrip(";")
    spiegazione = str(data.get("spiegazione", "")).strip()

    if not sql:
        raise ValueError("Il modello non ha restituito una query SQL.")

    return sql, spiegazione

def valida_select(sql: str):
    q = sql.strip().lower()
    if not q.startswith("select") and not q.startswith("with"):
        raise ValueError("Sono consentite solo query SELECT.")

    # Evita comandi multipli e operazioni di modifica.
    if ";" in q:
        raise ValueError("Sono consentite query singole.")
    for parola in _PAROLE_VIETATE_GRAFICO:
        if parola in q:
            raise ValueError(f"Query non permessa: contiene '{parola}'.")

    # Evita accesso a oggetti non appartenenti allo schema.
    # La validazione strutturale principale resta affidata allo schema fornito al modello.
    return sql

@st.cache_data(ttl=30, show_spinner=False)
def esegui_select_cache(sql: str):
    sql = valida_select(sql)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        colonne = [d[0] for d in cur.description]
        righe = cur.fetchall()
        return colonne, righe
    finally:
        conn.close()

def esegui_select(sql: str):
    return esegui_select_cache(sql)


st.set_page_config(page_title="Text2SQL - Oracle AI Agent", page_icon="🤖", layout="wide")

st.markdown("""
    <style>
    html, body, [data-testid="stAppViewContainer"], [data-testid="stAppViewContainer"] * ,
    [data-testid="stSidebar"], [data-testid="stSidebar"] * {
        color: #000000 !important;
    }
    [data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stSidebar"] {
        background-color: #FFFFFF !important;
    }
    [data-testid="stChatMessage"] * { color: #FFFFFF !important; }
    /* Eccezione: i campi di input dentro le bolle hanno sfondo chiaro, quindi testo nero */
    [data-testid="stChatMessage"] input,
    [data-testid="stChatMessage"] textarea,
    [data-testid="stChatMessage"] select,
    [data-testid="stChatMessage"] input::placeholder {
        color: #000000 !important;
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
        flex-direction: row-reverse !important;
        text-align: right !important;
        background-color: #2b313e !important;
        border-radius: 15px 15px 0px 15px !important;
        margin-left: auto !important;
        max-width: 80% !important;
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
        background-color: #1e222b !important;
        border-radius: 15px 15px 15px 0px !important;
        margin-right: auto !important;
        max-width: 80% !important;
    }
    [data-testid="stChatMessage"] [data-testid="stChatMessageAvatarUser"] {
        margin-left: 10px !important;
        margin-right: 0px !important;
    }
    button[kind="primary"], button[kind="primaryFormSubmit"] {
        color: #FFFFFF !important;
    }
    button[kind="primary"] *, button[kind="primaryFormSubmit"] * {
        color: #FFFFFF !important;
    }
    </style>
""", unsafe_allow_html=True)

try:
    # Verifica solo che le credenziali siano disponibili.
    get_allowed_tables()
except Exception as e:
    st.error(f"Errore di connessione al Database: {e}")
    st.stop()

if "all_chats" not in st.session_state:
    st.session_state.all_chats = {}

if "current_chat_id" not in st.session_state:
    new_id = str(uuid.uuid4())
    st.session_state.current_chat_id = new_id
    st.session_state.all_chats[new_id] = [
        {"role": "assistant", "content": "Ciao! Sono il tuo assistente Text-to-SQL. Posso eseguire query complesse con JOIN, fare analisi, oppure inserire, modificare o eliminare dati — sempre con conferma esplicita prima di ogni modifica."}
    ]

if "pending_write" not in st.session_state:
    st.session_state.pending_write = None

# Limite leggero per la demo di tesi.
# Evita che una singola sessione consumi rapidamente tutta la quota Gemini.
REQUEST_LIMIT = 30
REQUEST_WINDOW_SECONDS = 3600

if "request_times" not in st.session_state:
    st.session_state.request_times = deque()

def richiesta_consentita():
    now = time.time()
    while st.session_state.request_times and now - st.session_state.request_times[0] > REQUEST_WINDOW_SECONDS:
        st.session_state.request_times.popleft()

    if len(st.session_state.request_times) >= REQUEST_LIMIT:
        return False

    st.session_state.request_times.append(now)
    return True

def richieste_rimanenti():
    now = time.time()
    while st.session_state.request_times and now - st.session_state.request_times[0] > REQUEST_WINDOW_SECONDS:
        st.session_state.request_times.popleft()
    return max(0, REQUEST_LIMIT - len(st.session_state.request_times))


def nuova_chat(messaggio_iniziale):
    new_id = str(uuid.uuid4())
    st.session_state.current_chat_id = new_id
    st.session_state.all_chats[new_id] = [{"role": "assistant", "content": messaggio_iniziale}]
    st.session_state.pending_write = None


with st.sidebar:
    st.title("📜 Conversazioni")

    if st.button("➕ Nuova Conversazione", use_container_width=True, type="primary"):
        nuova_chat("Ciao! Nuova sessione avviata. Come posso aiutarti sul database Oracle?")
        st.rerun()

    st.markdown("---")
    st.subheader("Storico Chat")

    for cid in reversed(list(st.session_state.all_chats.keys())):
        messages = st.session_state.all_chats[cid]
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        title = (user_msgs[0][:25] + "...") if user_msgs else "Nuova Chat"

        col_chat, col_del = st.sidebar.columns([0.8, 0.2])
        is_active = (cid == st.session_state.current_chat_id)
        btn_label = f"🔹 {title}" if is_active else f"💬 {title}"

        if col_chat.button(btn_label, key=f"select_{cid}", use_container_width=True):
            st.session_state.current_chat_id = cid
            st.session_state.pending_write = None
            st.rerun()

        if col_del.button("🗑️", key=f"del_{cid}"):
            del st.session_state.all_chats[cid]
            if st.session_state.current_chat_id == cid:
                remaining = list(st.session_state.all_chats.keys())
                if remaining:
                    st.session_state.current_chat_id = remaining[-1]
                else:
                    nuova_chat("Ciao! Nuova sessione avviata.")
                st.session_state.pending_write = None
            st.rerun()

col_head1, col_head2 = st.columns([0.75, 0.25])
with col_head1:
    st.title("🤖 Text2SQL Assistant")
    st.caption(f"Richieste AI disponibili in questa sessione: **{richieste_rimanenti()}/{REQUEST_LIMIT}**")
with col_head2:
    st.write("")
    if st.button("🛑 Termina Sessione", use_container_width=True):
        nuova_chat("Sessione precedente terminata. Nuova conversazione avviata!")
        st.rerun()


current_messages = st.session_state.all_chats[st.session_state.current_chat_id]
for idx, msg in enumerate(current_messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            clean_text = msg["content"].replace("'", "\\'").replace("\n", " ")
            st.components.v1.html(
                f"""
                <script>
                function playMsg_{idx}() {{
                    if ('speechSynthesis' in window) {{
                        window.speechSynthesis.cancel();
                        var u = new SpeechSynthesisUtterance('{clean_text}');
                        u.lang = 'it-IT';
                        window.speechSynthesis.speak(u);
                    }}
                }}
                </script>
                <button onclick="playMsg_{idx}()" style="background:none;border:none;cursor:pointer;font-size:14px;margin-top:5px;color:#FFFFFF;font-weight:500;" title="Ascolta messaggio">
                    🔊 Ascolta
                </button>
                """,
                height=35,
            )

if st.session_state.get("last_query_result"):
    result = st.session_state["last_query_result"]
    st.markdown("### 📊 Risultato della query")
    st.dataframe(
        [dict(zip(result["columns"], row)) for row in result["rows"]],
        use_container_width=True
    )

if st.session_state.pending_write:
    pw = st.session_state.pending_write
    operazione = pw["operation"]

    with st.chat_message("assistant"):

        if operazione == "insert":
            st.warning(f"⚠️ Stai per inserire una nuova riga nella tabella **{pw['table']}**. Controlla i campi prima di confermare.")
            valori_finali = {}
            for col in pw["columns"]:
                name = col["name"]
                is_missing = name not in pw["provided"]
                col1, col2 = st.columns([3, 1])
                with col1:
                    default_val = "" if is_missing else str(pw["provided"][name])
                    val = st.text_input(
                        name.lower().replace("_", " "),
                        value=default_val,
                        key=f"field_{name}_{st.session_state.current_chat_id}",
                        disabled=is_missing and st.session_state.get(f"null_{name}_{st.session_state.current_chat_id}", True),
                    )
                with col2:
                    if is_missing:
                        is_null = st.checkbox("NULL", value=True, key=f"null_{name}_{st.session_state.current_chat_id}")
                    else:
                        is_null = False
                valori_finali[name] = None if is_null else val

            c1, c2 = st.columns(2)
            if c1.button("✅ Conferma ed esegui l'inserimento", use_container_width=True):
                try:
                    sql = execute_insert(pw["table"], valori_finali)
                    msg = f"Riga inserita correttamente nella tabella {pw['table']}.\n\nQuery eseguita: `{sql}`"
                    esegui_select_cache.clear()
                    esegui_select_cache.clear()
                    esegui_select_cache.clear()
                    current_messages.append({"role": "assistant", "content": msg})
                except Exception as e:
                    current_messages.append({"role": "assistant", "content": f"⚠️ Errore durante l'inserimento: {e}"})
                st.session_state.pending_write = None
                st.rerun()
            if c2.button("❌ Annulla", use_container_width=True):
                current_messages.append({"role": "assistant", "content": "Operazione annullata."})
                st.session_state.pending_write = None
                st.rerun()

        elif operazione == "update":
            st.warning(f"⚠️ Stai per modificare righe nella tabella **{pw['table']}**. Controlla campi e condizione prima di confermare.")

            st.markdown("**Campi da modificare** (lascia vuoto e spunta NULL per azzerare un campo):")
            set_finale = {}
            for name, value in pw["set"].items():
                col1, col2 = st.columns([3, 1])
                with col1:
                    is_null_default = value in (None, "", "NULL", "null")
                    val = st.text_input(
                        name.lower().replace("_", " "),
                        value="" if is_null_default else str(value),
                        key=f"set_{name}_{st.session_state.current_chat_id}",
                        disabled=st.session_state.get(f"setnull_{name}_{st.session_state.current_chat_id}", is_null_default),
                    )
                with col2:
                    is_null = st.checkbox("NULL", value=is_null_default, key=f"setnull_{name}_{st.session_state.current_chat_id}")
                set_finale[name] = None if is_null else val

            st.markdown("**Condizione (quali righe modificare):**")
            where_finale = {}
            for name, value in pw["where"].items():
                val = st.text_input(
                    f"dove {name.lower().replace('_', ' ')} =",
                    value=str(value) if value is not None else "",
                    key=f"where_{name}_{st.session_state.current_chat_id}",
                )
                where_finale[name] = val

            if not pw["set"]:
                st.info("Non ho riconosciuto campi da modificare. Riscrivi la richiesta specificando cosa cambiare.")
            if not pw["where"]:
                st.info("Non ho riconosciuto una condizione. Riscrivi la richiesta specificando come identificare le righe.")
            elif all(where_finale.values()):
                try:
                    colonne_prev, righe_prev = fetch_matching_rows(pw["table"], where_finale)
                    if righe_prev:
                        st.markdown("**Riga/e attualmente nel database (prima della modifica):**")
                        st.dataframe(
                            [dict(zip(colonne_prev, r)) for r in righe_prev],
                            use_container_width=True,
                        )
                    else:
                        st.warning("Nessuna riga corrisponde a questa condizione: verifica i valori inseriti.")
                except Exception as e:
                    st.error(f"Impossibile mostrare l'anteprima: {e}")

            c1, c2 = st.columns(2)
            if c1.button("✅ Conferma ed esegui la modifica", use_container_width=True, disabled=not (set_finale and where_finale and all(where_finale.values()))):
                try:
                    sql, righe = execute_update(pw["table"], set_finale, where_finale)
                    msg = f"{righe} riga/e modificata/e nella tabella {pw['table']}.\n\nQuery eseguita: `{sql}`"
                    current_messages.append({"role": "assistant", "content": msg})
                except Exception as e:
                    current_messages.append({"role": "assistant", "content": f"⚠️ Errore durante la modifica: {e}"})
                st.session_state.pending_write = None
                st.rerun()
            if c2.button("❌ Annulla", use_container_width=True):
                current_messages.append({"role": "assistant", "content": "Operazione annullata."})
                st.session_state.pending_write = None
                st.rerun()

        elif operazione == "delete":
            st.error(f"🗑️ Stai per **eliminare definitivamente** righe dalla tabella **{pw['table']}**. Questa operazione non è reversibile.")

            st.markdown("**Condizione (quali righe eliminare):**")
            where_finale = {}
            for name, value in pw["where"].items():
                val = st.text_input(
                    f"dove {name.lower().replace('_', ' ')} =",
                    value=str(value) if value is not None else "",
                    key=f"delwhere_{name}_{st.session_state.current_chat_id}",
                )
                where_finale[name] = val

            if not pw["where"]:
                st.info("Non ho riconosciuto una condizione. Riscrivi la richiesta specificando come identificare le righe da eliminare.")
            else:
                try:
                    conteggio = count_matching(pw["table"], where_finale)
                    st.warning(f"Questa condizione corrisponde a **{conteggio} riga/e**. Verranno eliminate tutte.")
                    if conteggio:
                        colonne_prev, righe_prev = fetch_matching_rows(pw["table"], where_finale)
                        st.markdown("**Riga/e che verranno eliminate:**")
                        st.dataframe(
                            [dict(zip(colonne_prev, r)) for r in righe_prev],
                            use_container_width=True,
                        )
                except Exception as e:
                    st.error(f"Impossibile verificare l'anteprima: {e}")

            conferma_esplicita = st.checkbox("Confermo di voler eliminare definitivamente queste righe", key=f"confirm_delete_{st.session_state.current_chat_id}")

            c1, c2 = st.columns(2)
            if c1.button("🗑️ Elimina definitivamente", use_container_width=True, disabled=not (where_finale and conferma_esplicita)):
                try:
                    sql, righe = execute_delete(pw["table"], where_finale)
                    msg = f"{righe} riga/e eliminata/e dalla tabella {pw['table']}.\n\nQuery eseguita: `{sql}`"
                    current_messages.append({"role": "assistant", "content": msg})
                except Exception as e:
                    current_messages.append({"role": "assistant", "content": f"⚠️ Errore durante l'eliminazione: {e}"})
                st.session_state.pending_write = None
                st.rerun()
            if c2.button("❌ Annulla", use_container_width=True):
                current_messages.append({"role": "assistant", "content": "Operazione annullata."})
                st.session_state.pending_write = None
                st.rerun()

pending_active = bool(st.session_state.pending_write)

with st.form(key="chat_form", clear_on_submit=True):
    c_input, c_mic, c_send = st.columns([10, 1, 1])

    with c_input:
        user_msg = st.text_input(
            "Messaggio",
            key="chat_text_input",
            label_visibility="collapsed",
            placeholder="Fai una domanda o chiedi una modifica al database...",
            disabled=pending_active,
        )

    with c_mic:
        st.components.v1.html(
            """
            <style>
            .mic-btn {
                width: 42px;
                height: 42px;
                border-radius: 50%;
                border: none;
                background-color: #25D366;
                color: #ffffff;
                font-size: 20px;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto;
            }
            .mic-btn.recording {
                background-color: #E8677D;
                animation: mic-glow 1s infinite;
            }
            @keyframes mic-glow {
                0%   { box-shadow: 0 0 0 4px rgba(232, 103, 125, 0.35); }
                50%  { box-shadow: 0 0 0 8px rgba(232, 103, 125, 0.55); }
                100% { box-shadow: 0 0 0 4px rgba(232, 103, 125, 0.35); }
            }
            body { margin: 0; display: flex; justify-content: center; align-items: center; }
            </style>
            <button id="micButton" type="button" class="mic-btn" onclick="startDictation()" title="Parla con il microfono">🎤</button>
            <script>
            function startDictation() {
                const btn = document.getElementById('micButton');
                if (window.hasOwnProperty('webkitSpeechRecognition')) {
                    var recognition = new webkitSpeechRecognition();
                    recognition.continuous = false;
                    recognition.interimResults = false;
                    recognition.lang = "it-IT";

                    recognition.onstart = function() { if (btn) btn.classList.add('recording'); };
                    recognition.onend = function() { if (btn) btn.classList.remove('recording'); };
                    recognition.onerror = function() { if (btn) btn.classList.remove('recording'); };
                    recognition.onresult = function(e) {
                        var transcript = e.results[0][0].transcript;
                        var input = window.parent.document.querySelector('input[aria-label="Messaggio"]');
                        if (input) {
                            const setter = Object.getOwnPropertyDescriptor(window.parent.HTMLInputElement.prototype, 'value').set;
                            setter.call(input, transcript);
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    };
                    recognition.start();
                } else {
                    alert('Il riconoscimento vocale non è supportato da questo browser.');
                }
            }
            </script>
            """,
            height=45,
        )

    with c_send:
        submit = st.form_submit_button("Invia", type="primary", use_container_width=True, disabled=pending_active)

if submit and user_msg.strip():
    current_messages.append({"role": "user", "content": user_msg})

    # I saluti non consumano richieste AI.
    if _is_simple_message(user_msg):
        current_messages.append({"role": "assistant", "content": risposta_semplice(user_msg)})
        st.rerun()

    # Per una demo di tesi limitiamo il consumo della quota per sessione.
    if not richiesta_consentita():
        current_messages.append({
            "role": "assistant",
            "content": f"⚠️ Hai raggiunto il limite di {REQUEST_LIMIT} richieste AI nell'ultima ora per questa sessione. "
                       "Il limite serve a proteggere la quota dell'app durante la demo."
        })
        st.rerun()

    with st.spinner("Elaborazione della richiesta in corso..."):
        try:
            # Una sola chiamata AI per le richieste di lettura.
            sql, spiegazione = genera_sql_lettura(user_msg)
            sql = valida_select(sql)

            colonne, righe = esegui_select(sql)

            if spiegazione:
                output_text = spiegazione + "\n\n"
            else:
                output_text = ""

            output_text += f"**Risultato:** {len(righe)} righe trovate.\n\n"

            if righe:
                st.session_state[f"result_{len(current_messages)}"] = (
                    colonne, righe
                )
                # Il dataframe viene mostrato tramite un messaggio sintetico;
                # il risultato viene visualizzato subito sotto.
                output_text += f"```sql\n{sql}\n```"
            else:
                output_text += f"```sql\n{sql}\n```"

            current_messages.append({"role": "assistant", "content": output_text})

            # Mostra il risultato in modo immediato.
            if righe:
                st.session_state["last_query_result"] = {
                    "columns": colonne,
                    "rows": righe
                }

        except Exception as e:
            current_messages.append({
                "role": "assistant",
                "content": f"⚠️ Si è verificato un errore durante l'elaborazione: {e}"
            })

    st.rerun()