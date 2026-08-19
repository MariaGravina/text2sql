"""
Text2SQL Assistant — Streamlit, file unico.

Richiede nella stessa cartella:
  - un file .env con GOOGLE_API_KEY, ORACLE_ADMIN_PASSWORD, ORACLE_WALLET_PASSWORD
  - la cartella Wallet_text2sqldb (wallet Oracle)

Avvio: streamlit run app.py
"""

import os
import re
import json
import uuid
import warnings

# Forza oracledb ad usare SOLO la modalità Thin in Python puro (evita errori di blocco DLL su Windows)
os.environ["PYORACLEDB_THIN_MODE"] = "1"

import streamlit as st
import oracledb

# Mantiene la modalità Thin nativa
oracledb.init_oracle_client = None

from dotenv import load_dotenv
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import create_sql_agent
from langchain_google_genai import ChatGoogleGenerativeAI

warnings.filterwarnings("ignore")
load_dotenv()


def apri_porta_firewall(porta: int = 8501):
    """
    Tenta di creare automaticamente una regola nel Firewall di Windows per
    permettere le connessioni in ingresso da altri PC della rete locale.
    """
    import platform
    import subprocess

    if platform.system() != "Windows":
        return  # rilevante solo su Windows

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
            print(f"✅ Regola firewall creata automaticamente per la porta {porta}.")
        else:
            print(f"✅ Regola firewall per la porta {porta} già presente.")
    except Exception:
        print(
            f"⚠️  Non sono riuscito ad aprire automaticamente la porta {porta} nel firewall.\n"
        )


apri_porta_firewall()

# =========================================================
# CONFIGURAZIONE
# =========================================================
REQUIRED_ENV_VARS = ["GOOGLE_API_KEY", "ORACLE_ADMIN_PASSWORD", "ORACLE_WALLET_PASSWORD"]
_mancanti = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if _mancanti:
    st.error(f"❌ Variabili mancanti nel file .env o nei Secrets: {', '.join(_mancanti)}")
    st.stop()

DB_USER = "ADMIN"
DB_PASSWORD = os.environ["ORACLE_ADMIN_PASSWORD"]
WALLET_PASSWORD = os.environ["ORACLE_WALLET_PASSWORD"]
DSN = "text2sqldb_high"
WALLET_DIR = os.path.abspath("Wallet_text2sqldb")

# Modelli Gemini con piano Free esteso
MODEL_SIMPLE = "gemini-2.5-flash-lite"
MODEL_AGENT = "gemini-2.0-flash"


# =========================================================
# ACCESSO A ORACLE (letture di schema + scritture sicure)
# =========================================================
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
    """Elenco delle tabelle realmente presenti nello schema (whitelist)."""
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
    """Colonne reali di una tabella (nome, nullable, tipo), validando il nome tabella."""
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
    """Esegue un INSERT parametrizzato e sicuro."""
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
    """Esegue un UPDATE parametrizzato e sicuro."""
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


def count_matching(table_name: str, where_fields: dict) -> int:
    """Conta quante righe soddisfano una condizione."""
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
    """Esegue un DELETE parametrizzato e sicuro."""
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


# =========================================================
# GEMINI: classificazione intento + estrazione campi
# =========================================================
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
  "intento": "read" | "insert" | "update" | "delete" | "altro",
  "tabella": "NOME_TABELLA_O_NULL",
  "campi": {{"NOME_COLONNA": "valore", ...}},
  "where": {{"NOME_COLONNA": "valore", ...}}
}}

Regole:
- "read": l'utente vuole solo interrogare/leggere dati (ricerche, conteggi, join, statistiche, analisi).
- "insert": l'utente vuole aggiungere una nuova riga. Usa "campi" per i valori forniti, "where" vuoto.
- "update": l'utente vuole modificare righe esistenti. Usa "campi" per i nuovi valori, "where" per identificare le righe.
- "delete": l'utente vuole eliminare righe esistenti. Usa "where" per identificare le righe, "campi" vuoto.
- "altro": la richiesta NON riguarda questo database (saluti generici, chiacchiere, argomenti non pertinenti).
- Per "read" imposta comunque "tabella" a null e lascia "campi"/"where" vuoti.
- Non inventare valori non menzionati dall'utente."""

    risposta = _llm().invoke(prompt)
    testo = _testo(risposta.content).strip()
    testo = re.sub(r"^```(json)?|```$", "", testo, flags=re.MULTILINE).strip()
    try:
        dati = json.loads(testo)
    except json.JSONDecodeError:
        dati = {}

    intento = str(dati.get("intento", "read")).strip().lower()
    if intento not in ("read", "insert", "update", "delete", "altro"):
        intento = "read"

    return {
        "intento": intento,
        "tabella": dati.get("tabella") or None,
        "campi": dati.get("campi", {}) or {},
        "where": dati.get("where", {}) or {},
    }


# =========================================================
# AGENTE DI SOLA LETTURA (SELECT)
# =========================================================
PREFIX_PROMPT = """Sei un agente SQL esperto per un database Oracle, specializzato in dati clinici cardiologici.
Rispondi sempre in italiano, in modo chiaro e comprensibile anche per un utente non tecnico.
Sei abilitato SOLO a leggere dati (SELECT), anche con JOIN, aggregazioni, filtri e sotto-query complesse.
Prima di eseguire una query su una tabella, verifica sempre la struttura delle tabelle rilevanti (colonne, tipi).
Se la domanda è ambigua, fai la scelta più ragionevole e spiega brevemente l'assunzione fatta nella risposta finale.
"""


def estrai_testo_risposta(output):
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parti = [b.get("text", "") for b in output if isinstance(b, dict) and b.get("type") == "text"]
        if parti:
            return "\n".join(parti)
    return str(output)


@st.cache_resource
def get_read_agent():
    from urllib.parse import quote_plus

    encoded_user = quote_plus(DB_USER)
    encoded_password = quote_plus(DB_PASSWORD)
    encoded_wallet_password = quote_plus(WALLET_PASSWORD)
    connection_string = (
        f"oracle+oracledb://{encoded_user}:{encoded_password}@{DSN}"
        f"?config_dir={WALLET_DIR}&wallet_location={WALLET_DIR}&wallet_password={encoded_wallet_password}"
    )
    db = SQLDatabase.from_uri(connection_string, sample_rows_in_table_info=2)
    llm = ChatGoogleGenerativeAI(model=MODEL_AGENT, temperature=0)
    return create_sql_agent(
        llm=llm,
        db=db,
        verbose=True,
        prefix=PREFIX_PROMPT,
        agent_type="tool-calling",
        handle_parsing_errors=True,
    )


# =========================================================
# INTERFACCIA STREAMLIT
# =========================================================
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
    agent = get_read_agent()
except Exception as e:
    st.error(f"Errore di connessione al Database o API Gemini: {e}")
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


def nuova_chat(messaggio_iniziale):
    new_id = str(uuid.uuid4())
    st.session_state.current_chat_id = new_id
    st.session_state.all_chats[new_id] = [{"role": "assistant", "content": messaggio_iniziale}]
    st.session_state.pending_write = None


# --- SIDEBAR ---
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

# --- HEADER ---
col_head1, col_head2 = st.columns([0.75, 0.25])
with col_head1:
    st.title("🤖 Text2SQL Assistant")
with col_head2:
    st.write("")
    if st.button("🛑 Termina Sessione", use_container_width=True):
        nuova_chat("Sessione precedente terminata. Nuova conversazione avviata!")
        st.rerun()


# --- STORICO MESSAGGI ---
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

# --- CARD DI CONFERMA PER SCRITTURE IN SOSPESO ---
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

            st.markdown("**Campi da modificare:**")
            set_finale = {}
            for name, value in pw["set"].items():
                val = st.text_input(
                    name.lower().replace("_", " "),
                    value=str(value) if value is not None else "",
                    key=f"set_{name}_{st.session_state.current_chat_id}",
                )
                set_finale[name] = val

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

            c1, c2 = st.columns(2)
            if c1.button("✅ Conferma ed esegui la modifica", use_container_width=True, disabled=not (set_finale and where_finale)):
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

# --- INPUT UTENTE ---
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
                        var input = window.parent.document.querySelector('input[placeholder="Fai una domanda o chiedi una modifica al database..."]');
                        if (input) {
                            input.value = transcript;
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

    with st.spinner("Elaborazione della richiesta in corso..."):
        try:
            tabelle = get_allowed_tables()
            analisi = analizza_richiesta(user_msg, tabelle)
            intento = analisi["intento"]
            tabella = analisi["tabella"]

            if intento == "altro":
                risposta = "Non riesco ad elaborare questa richiesta. Posso aiutarti solo con consultazioni e modifiche al database Oracle."
                current_messages.append({"role": "assistant", "content": risposta})

            elif intento in ("insert", "update", "delete"):
                if not tabella or tabella.upper() not in [t.upper() for t in tabelle]:
                    current_messages.append({
                        "role": "assistant",
                        "content": f"Non ho identificato una tabella valida per questa operazione. Tabelle disponibili: {', '.join(tabelle)}"
                    })
                else:
                    tabella_real = [t for t in tabelle if t.upper() == tabella.upper()][0]
                    if intento == "insert":
                        cols = get_table_columns(tabella_real)
                        st.session_state.pending_write = {
                            "operation": "insert",
                            "table": tabella_real,
                            "columns": cols,
                            "provided": analisi["campi"],
                        }
                    elif intento == "update":
                        st.session_state.pending_write = {
                            "operation": "update",
                            "table": tabella_real,
                            "set": analisi["campi"],
                            "where": analisi["where"],
                        }
                    elif intento == "delete":
                        st.session_state.pending_write = {
                            "operation": "delete",
                            "table": tabella_real,
                            "where": analisi["where"],
                        }

            else:  # 'read'
                res = agent.invoke({"input": user_msg})
                output_text = estrai_testo_risposta(res.get("output", ""))
                current_messages.append({"role": "assistant", "content": output_text})

        except Exception as e:
            current_messages.append({"role": "assistant", "content": f"⚠️ Si è verificato un errore durante l'elaborazione: {e}"})

    st.rerun()