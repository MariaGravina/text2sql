


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
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "No rules match" in check.stdout or check.returncode != 0:
            subprocess.run(
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    f"name={nome_regola}",
                    "dir=in",
                    "action=allow",
                    "protocol=TCP",
                    f"localport={porta}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
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

MODEL_SIMPLE = "gemini-3.1-flash-lite"
MODEL_AGENT = "gemini-3.1-flash-lite"

_allowed_tables_cache = None
_columns_cache = {}

_LLM_CACHE = {}
_LLM_CACHE_TTL_SECONDS = 300
_SCHEMA_PROMPT_CACHE = None

MAX_AI_REQUESTS_PER_SESSION = 15


def _cache_key(model: str, prompt: str) -> str:
    import hashlib
    return hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest()


def _llm(model=None):
    return ChatGoogleGenerativeAI(model=model or MODEL_SIMPLE)


def _testo(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parti = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parti)
    return str(content)


def _llm_invoke_cached(prompt: str, model=None):
    """
    Esegue una sola chiamata LLM quando necessario.
    Le richieste identiche vengono servite dalla cache per 5 minuti.
    """
    import time

    model_name = model or MODEL_SIMPLE
    key = _cache_key(model_name, prompt)
    now = time.time()

    cached = _LLM_CACHE.get(key)
    if cached and now - cached["time"] < _LLM_CACHE_TTL_SECONDS:
        return cached["content"]

    try:
        risposta = _llm(model_name).invoke(prompt)
        content = _testo(risposta.content).strip()
    except Exception as exc:
        message = str(exc)
        if "429" in message or "RESOURCE_EXHAUSTED" in message:
            raise RuntimeError(
                "Gemini ha raggiunto il limite di richieste disponibile per il "
                "progetto. Nessun retry automatico è stato effettuato per evitare "
                "di consumare ulteriormente la quota."
            ) from exc
        raise

    _LLM_CACHE[key] = {"time": now, "content": content}

    if len(_LLM_CACHE) > 500:
        oldest_key = min(_LLM_CACHE, key=lambda k: _LLM_CACHE[k]["time"])
        _LLM_CACHE.pop(oldest_key, None)

    return content


def _consume_ai_request():
    used = st.session_state.get("ai_requests", 0)
    if used >= MAX_AI_REQUESTS_PER_SESSION:
        raise RuntimeError(
            f"Hai raggiunto il limite di {MAX_AI_REQUESTS_PER_SESSION} "
            "richieste AI in questa sessione."
        )
    st.session_state.ai_requests = used + 1


def _is_simple_message(text: str) -> bool:
    normalized = re.sub(r"[^a-zàèéìòù0-9 ]", "", text.lower()).strip()
    return normalized in {
        "ciao",
        "salve",
        "buongiorno",
        "buonasera",
        "buonanotte",
        "hey",
        "grazie",
        "ok",
        "okay",
        "perfetto",
        "va bene",
    }


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
        cols = [
            {"name": r[0], "nullable": (r[1] == "Y"), "type": r[2]}
            for r in cur.fetchall()
        ]
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

    values = [clean_set[c] for c in set_cols]
    bind_index = len(values) + 1
    where_parts = []

    for c in where_cols:
        if clean_where[c] is None:
            where_parts.append(f"{c} IS NULL")
        else:
            where_parts.append(f"{c} = :{bind_index}")
            values.append(clean_where[c])
            bind_index += 1

    where_clause = " AND ".join(where_parts)
    sql = f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}"

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

    values = []
    where_parts = []
    bind_index = 1

    for c, value in clean_where.items():
        if value is None:
            where_parts.append(f"{c} IS NULL")
        else:
            where_parts.append(f"{c} = :{bind_index}")
            values.append(value)
            bind_index += 1

    where_clause = " AND ".join(where_parts)

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

    values = []
    where_parts = []
    bind_index = 1

    for c, value in clean_where.items():
        if value is None:
            where_parts.append(f"{c} IS NULL")
        else:
            where_parts.append(f"{c} = :{bind_index}")
            values.append(value)
            bind_index += 1

    where_clause = " AND ".join(where_parts)

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

    values = []
    where_parts = []
    bind_index = 1

    for c, value in clean_where.items():
        if value is None:
            where_parts.append(f"{c} IS NULL")
        else:
            where_parts.append(f"{c} = :{bind_index}")
            values.append(value)
            bind_index += 1

    where_clause = " AND ".join(where_parts)
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


def valida_select_sql(query: str) -> bool:
    """Permette esclusivamente una singola SELECT di sola lettura."""
    if not isinstance(query, str):
        return False

    q = query.strip()
    q_lower = q.lower()

    if not q_lower.startswith("select"):
        return False

    if ";" in q or "--" in q or "/*" in q or "*/" in q:
        return False

    parole_bloccate = (
        " insert ",
        " update ",
        " delete ",
        " drop ",
        " alter ",
        " truncate ",
        " merge ",
        " grant ",
        " revoke ",
        " create ",
        " commit ",
        " rollback ",
        " execute ",
        " begin ",
        " declare ",
    )
    padded = f" {q_lower} "

    return not any(parola in padded for parola in parole_bloccate)


def esegui_query_sicura_sola_lettura(query: str):
    """Esegue esclusivamente una SELECT validata."""
    query = query.strip().rstrip(";").strip()

    if not valida_select_sql(query):
        raise ValueError("La query generata non è una SELECT di sola lettura consentita.")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(query)
        colonne = [d[0] for d in cur.description]
        righe = cur.fetchall()
        return colonne, righe
    finally:
        conn.close()


def formatta_risultato_query(colonne, righe, query) -> str:
    if not righe:
        testo = "Risultato: nessuna riga trovata."
    else:
        testo = f"Risultato: {len(righe)} righe trovate."

    return f"{testo}\n\n**Query SQL generata:**\n\n```sql\n{query}\n```"


def mostra_risultato_query(colonne, righe, query, titolo="Risultato"):
    import pandas as pd

    st.markdown(f"### 📋 {titolo}")

    if colonne:
        df = pd.DataFrame(righe or [], columns=colonne)
        st.dataframe(df, use_container_width=True, hide_index=True)

        if not df.empty:
            st.markdown("### 📊 Rappresentazione grafica")
            df_chart = df.copy()

            for col in df_chart.columns:
                converted = pd.to_numeric(df_chart[col], errors="coerce")
                if converted.notna().all():
                    df_chart[col] = converted

            numeric_cols = list(df_chart.select_dtypes(include="number").columns)
            categorical_cols = [c for c in df_chart.columns if c not in numeric_cols]

            if numeric_cols:
                if categorical_cols:
                    index_col = categorical_cols[0]
                    chart = df_chart[[index_col] + numeric_cols].head(50).copy()
                    chart[index_col] = chart[index_col].astype(str)
                    chart = chart.set_index(index_col)
                    st.bar_chart(chart, use_container_width=True)
                elif len(numeric_cols) == 1:
                    st.bar_chart(df_chart[numeric_cols].head(50), use_container_width=True)
                else:
                    st.line_chart(df_chart[numeric_cols].head(50), use_container_width=True)
            else:
                st.info("Non ci sono valori numerici sufficienti per creare automaticamente un grafico.")

    with st.expander("🔎 Mostra/nascondi SQL generata"):
        st.code(query, language="sql")


def _schema_per_prompt(tabelle_disponibili: list) -> str:
    """
    Schema inviato a Gemini con nome colonna, tipo Oracle e nullability.
    Questo evita che il modello usi EXTRACT su NUMBER/VARCHAR2.
    """
    global _SCHEMA_PROMPT_CACHE

    if _SCHEMA_PROMPT_CACHE is not None:
        return _SCHEMA_PROMPT_CACHE

    righe = []

    for tabella in tabelle_disponibili:
        try:
            colonne = get_table_columns(tabella)
            descrizione_colonne = []

            for c in colonne:
                nome = str(c.get("name", ""))
                tipo = str(c.get("type", ""))
                nullable = "NULL" if c.get("nullable") else "NOT NULL"
                descrizione_colonne.append(f"{nome} [{tipo}, {nullable}]")

            righe.append(f"{tabella}: " + ", ".join(descrizione_colonne))

        except Exception:
            righe.append(f"{tabella}: colonne non disponibili")

    _SCHEMA_PROMPT_CACHE = "\n".join(righe)
    return _SCHEMA_PROMPT_CACHE


def analizza_richiesta(domanda: str, tabelle_disponibili: list) -> dict:
    schema = _schema_per_prompt(tabelle_disponibili)

    prompt = f"""Sei il motore Text2SQL di un'applicazione Streamlit che interroga
un database Oracle di dati clinici.

RICHIESTA UTENTE:
"{domanda}"

SCHEMA ORACLE DISPONIBILE:
{schema}

Rispondi SOLO con JSON valido, senza markdown e senza testo esterno.

Formato obbligatorio:
{{
  "intento": "read" | "insert" | "update" | "delete" | "grafico" | "altro",
  "tabella": "NOME_TABELLA_O_NULL",
  "campi": {{}},
  "where": {{}},
  "solo_domanda": true,
  "sql": "SELECT ... oppure null"
}}

REGOLE:
1. read = richiesta di lettura, ricerca, conteggio, statistica, JOIN o analisi.
2. grafico = l'utente chiede esplicitamente una rappresentazione grafica,
   un andamento, un diagramma, un istogramma o una visualizzazione.
3. insert/update/delete = modifica reale dei dati.
4. altro = saluti o richieste non pertinenti al database.
5. Per read e grafico, "sql" DEVE contenere una singola SELECT Oracle valida.
6. Per insert/update/delete/altro, "sql" deve essere null.
7. Usa SOLO tabelle e colonne presenti nello schema.
8. Non inventare nomi di colonne.
9. Una SELECT deve essere di sola lettura.
10. Non usare INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, MERGE,
    GRANT, REVOKE, CREATE, COMMIT o ROLLBACK.
11. Se l'utente chiede un conteggio, restituisci una query aggregata.
12. Per un grafico, preferisci 2 colonne: categoria/data + valore numerico.
12.1 Per richieste relative all'età:
    - se esiste una colonna ETA, ETÀ o equivalente di tipo NUMBER, usa direttamente quella colonna;
    - NON usare EXTRACT su colonne NUMBER;
    - se esiste una DATA_NASCITA di tipo DATE o TIMESTAMP, calcola l'età con
      TRUNC(MONTHS_BETWEEN(SYSDATE, DATA_NASCITA) / 12);
    - usa EXTRACT(YEAR FROM ...) solo quando la colonna sorgente è effettivamente DATE o TIMESTAMP;
    - non usare EXTRACT su VARCHAR2, NUMBER o altri tipi incompatibili;
    - se esiste una colonna numerica che rappresenta già l'età, preferiscila alla data di nascita.
12.2 Controlla SEMPRE il tipo Oracle della colonna prima di usare funzioni come
     EXTRACT, MONTHS_BETWEEN, TO_DATE, TO_NUMBER o funzioni matematiche.
12.3 Non applicare funzioni data/ora a colonne NUMBER o VARCHAR2 senza una conversione
     esplicitamente giustificata dal tipo e dal contenuto della colonna.
13. Per risultati dettagliati molto grandi, usa FETCH FIRST 1000 ROWS ONLY.
14. Non aggiungere punto e virgola alla SQL.
15. Non inventare valori per INSERT/UPDATE/DELETE.
16. "solo_domanda" è true solo quando l'utente chiede quali dati servono,
    senza chiedere di eseguire una modifica.

Esempio di grafico:
{{
  "intento": "grafico",
  "tabella": null,
  "campi": {{}},
  "where": {{}},
  "solo_domanda": false,
  "sql": "SELECT ... FROM ... GROUP BY ... ORDER BY ..."
}}
"""

    testo = _llm_invoke_cached(prompt, MODEL_SIMPLE).strip()
    testo = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        testo,
        flags=re.IGNORECASE,
    ).strip()

    try:
        dati = json.loads(testo)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", testo, flags=re.DOTALL)
        if not match:
            raise ValueError("Gemini non ha restituito un JSON valido.")
        dati = json.loads(match.group(0))

    intento = str(dati.get("intento", "read")).strip().lower()
    if intento not in ("read", "insert", "update", "delete", "grafico", "altro"):
        intento = "read"

    sql = dati.get("sql")
    if isinstance(sql, str):
        sql = sql.replace("```sql", "").replace("```", "").strip().rstrip(";").strip()
    else:
        sql = None

    return {
        "intento": intento,
        "tabella": dati.get("tabella") or None,
        "campi": dati.get("campi", {}) or {},
        "where": dati.get("where", {}) or {},
        "solo_domanda": bool(dati.get("solo_domanda", False)),
        "sql": sql,
    }


def correggi_query_sql_se_necessario(
    domanda: str,
    query: str,
    errore: Exception,
    tabelle_disponibili: list,
) -> str:
    """
    Corregge una SELECT che Oracle ha rifiutato.
    Viene usata solo dopo un vero errore SQL, evitando retry inutili.
    """
    schema = _schema_per_prompt(tabelle_disponibili)

    prompt = f"""Sei un esperto Oracle SQL.

RICHIESTA ORIGINALE DELL'UTENTE:
"{domanda}"

SCHEMA ORACLE:
{schema}

QUERY CHE HA GENERATO ERRORE:
{query}

ERRORE ORACLE:
{str(errore)}

Correggi la query mantenendo esattamente lo stesso obiettivo della richiesta.

REGOLE OBBLIGATORIE:
1. Restituisci SOLO una singola SELECT Oracle.
2. Usa esclusivamente tabelle e colonne presenti nello schema.
3. Non inventare colonne.
4. Controlla attentamente il tipo Oracle di ogni colonna.
5. NON usare EXTRACT su NUMBER o VARCHAR2.
6. EXTRACT può essere usato solo su tipi DATE/TIMESTAMP compatibili.
7. Se l'età è già memorizzata in una colonna NUMBER, usa direttamente quella colonna.
8. Se l'età deve essere calcolata da una colonna DATE/TIMESTAMP di nascita,
   usa TRUNC(MONTHS_BETWEEN(SYSDATE, colonna_data_nascita) / 12).
9. Non usare INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, MERGE,
   GRANT, REVOKE, CREATE, COMMIT o ROLLBACK.
10. Non aggiungere il punto e virgola finale.
11. Per un grafico restituisci preferibilmente categoria + valore numerico.
12. Mantieni eventuali GROUP BY e ORDER BY coerenti con la SELECT.

Restituisci SOLO la query SQL corretta."""

    query_corretta = _llm_invoke_cached(prompt, MODEL_SIMPLE).strip()

    query_corretta = re.sub(
        r"^```(?:sql)?\s*|\s*```$",
        "",
        query_corretta,
        flags=re.IGNORECASE,
    ).strip().rstrip(";").strip()

    if not valida_select_sql(query_corretta):
        raise ValueError("La query SQL corretta generata da Gemini non è valida o non è di sola lettura.")

    return query_corretta


def descrivi_campi_tabella(tabella: str, colonne: list) -> str:
    righe = []
    for c in colonne:
        obbligatorio = "obbligatorio" if not c["nullable"] else "facoltativo"
        righe.append(f"- **{c['name'].lower()}** ({c['type'].lower()}, {obbligatorio})")

    return (
        f"Per un'operazione sulla tabella **{tabella}**, questi sono i campi disponibili:\n\n"
        + "\n".join(righe)
        + "\n\nDimmi i valori che vuoi usare e preparo l'operazione con conferma prima di eseguirla."
    )


def genera_risposta_generica(domanda: str) -> str:
    prompt = f"""Sei l'assistente di un'applicazione per interrogare e gestire un database Oracle
di dati clinici cardiologici (pazienti, esami, visite, coronarografie, ecc.).

L'utente ha scritto questo messaggio, che non sembra riguardare direttamente il database: "{domanda}"

Rispondi in modo breve, naturale e cordiale in italiano (massimo 2-3 frasi)."""
    return _llm_invoke_cached(prompt).strip()


PREFIX_PROMPT = """Sei un agente SQL esperto per un database Oracle, specializzato in dati clinici cardiologici.
Rispondi sempre in italiano, in modo chiaro e comprensibile anche per un utente non tecnico.
Sei abilitato SOLO a leggere dati (SELECT), anche con JOIN, aggregazioni, filtri e sotto-query complesse.
Prima di eseguire una query su una tabella, verifica sempre la struttura delle tabelle rilevanti (colonne, tipi).
Se la domanda è ambigua, fai la scelta più ragionevole e spiega brevemente l'assunzione fatta nella risposta finale.
"""


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
    llm = ChatGoogleGenerativeAI(model=MODEL_AGENT)

    return create_sql_agent(
        llm=llm,
        db=db,
        verbose=True,
        prefix=PREFIX_PROMPT,
        agent_type="tool-calling",
        handle_parsing_errors=True,
    )


st.set_page_config(
    page_title="Text2SQL - Oracle AI Agent",
    page_icon="🤖",
    layout="wide",
)

st.markdown(
    """
    <style>
    /* Sfondo generale */
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    [data-testid="stSidebar"] {
        background-color: #FFFFFF !important;
    }

    /* Testo generale nero fuori dalle chat */
    [data-testid="stAppViewContainer"] > .main,
    [data-testid="stSidebar"] {
        color: #000000 !important;
    }

    /* Bolla utente */
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
        flex-direction: row-reverse !important;
        text-align: left !important;
        background-color: #2b313e !important;
        border-radius: 15px 15px 0px 15px !important;
        margin-left: auto !important;
        max-width: 80% !important;
    }

    /* Bolla assistente */
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
        background-color: #1e222b !important;
        border-radius: 15px 15px 15px 0px !important;
        margin-right: auto !important;
        max-width: 80% !important;
    }

    /* Testo normale nelle bolle chat bianco */
    [data-testid="stChatMessage"] p,
    [data-testid="stChatMessage"] li,
    [data-testid="stChatMessage"] h1,
    [data-testid="stChatMessage"] h2,
    [data-testid="stChatMessage"] h3,
    [data-testid="stChatMessage"] h4,
    [data-testid="stChatMessage"] strong,
    [data-testid="stChatMessage"] em,
    [data-testid="stChatMessage"] span:not([data-testid="stCodeBlock"] span) {
        color: #FFFFFF !important;
    }

    /* Riquadri SQL/codice: sfondo bianco e testo nero.
       Questa regola viene dopo quella delle chat, quindi ha priorità. */
    [data-testid="stChatMessage"] [data-testid="stCodeBlock"],
    [data-testid="stChatMessage"] [data-testid="stCodeBlock"] *,
    [data-testid="stChatMessage"] pre,
    [data-testid="stChatMessage"] pre *,
    [data-testid="stChatMessage"] code,
    [data-testid="stChatMessage"] code * {
        color: #000000 !important;
        background-color: #FFFFFF !important;
    }

    [data-testid="stCodeBlock"] {
        background-color: #FFFFFF !important;
        border-radius: 8px !important;
    }

    [data-testid="stCodeBlock"] pre,
    [data-testid="stCodeBlock"] code,
    [data-testid="stCodeBlock"] span {
        color: #000000 !important;
        background-color: #FFFFFF !important;
    }

    /* Campi input: sfondo bianco + testo nero */
    input,
    textarea,
    select,
    [data-baseweb="input"] input {
        color: #000000 !important;
        background-color: #FFFFFF !important;
    }

    input::placeholder,
    textarea::placeholder {
        color: #666666 !important;
        opacity: 1 !important;
    }

    /* Dataframe */
    [data-testid="stDataFrame"] {
        color: #000000 !important;
    }

    [data-testid="stChatMessage"] [data-testid="stChatMessageAvatarUser"] {
        margin-left: 10px !important;
        margin-right: 0px !important;
    }

    /* Pulsanti primari */
    button[kind="primary"],
    button[kind="primaryFormSubmit"],
    button[kind="primary"] *,
    button[kind="primaryFormSubmit"] * {
        color: #FFFFFF !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

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
        {
            "role": "assistant",
            "content": (
                "Ciao! Sono il tuo assistente Text-to-SQL. Posso eseguire query complesse con JOIN, "
                "fare analisi, oppure inserire, modificare o eliminare dati — sempre con conferma "
                "esplicita prima di ogni modifica."
            ),
        }
    ]

if "pending_write" not in st.session_state:
    st.session_state.pending_write = None

if "ai_requests" not in st.session_state:
    st.session_state.ai_requests = 0

if "pending_chart" not in st.session_state:
    st.session_state.pending_chart = None

if "last_result" not in st.session_state:
    st.session_state.last_result = None


def nuova_chat(messaggio_iniziale, reset_ai_counter=False):
    new_id = str(uuid.uuid4())
    st.session_state.current_chat_id = new_id
    st.session_state.all_chats[new_id] = [
        {"role": "assistant", "content": messaggio_iniziale}
    ]
    st.session_state.pending_write = None
    st.session_state.pending_chart = None
    st.session_state.last_result = None

    if reset_ai_counter:
        st.session_state.ai_requests = 0


with st.sidebar:
    st.title("📜 Conversazioni")

    if st.button("➕ Nuova Conversazione", use_container_width=True, type="primary"):
        nuova_chat("Ciao! Nuova sessione avviata. Come posso aiutarti sul database Oracle?")
        st.rerun()

    st.markdown("---")
    st.subheader("Storico Chat")
    st.caption(
        f"Richieste AI sessione: "
        f"{st.session_state.get('ai_requests', 0)}/{MAX_AI_REQUESTS_PER_SESSION}"
    )

    for cid in reversed(list(st.session_state.all_chats.keys())):
        messages = st.session_state.all_chats[cid]
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        title = (user_msgs[0][:25] + "...") if user_msgs else "Nuova Chat"

        col_chat, col_del = st.sidebar.columns([0.8, 0.2])
        is_active = cid == st.session_state.current_chat_id
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

with col_head2:
    st.write("")
    if st.button("🛑 Termina Sessione", use_container_width=True):
        nuova_chat(
            "Sessione precedente terminata. Nuova conversazione avviata!",
            reset_ai_counter=True,
        )
        st.rerun()


current_messages = st.session_state.all_chats[st.session_state.current_chat_id]

for idx, msg in enumerate(current_messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

        if msg["role"] == "assistant":
            clean_text = (
                msg["content"]
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\n", " ")
            )

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
                <button onclick="playMsg_{idx}()"
                    style="background:none;border:none;cursor:pointer;font-size:14px;
                    margin-top:5px;color:#FFFFFF;font-weight:500;"
                    title="Ascolta messaggio">
                    🔊 Ascolta
                </button>
                """,
                height=35,
            )


if st.session_state.pending_write:
    pw = st.session_state.pending_write
    operazione = pw["operation"]

    with st.chat_message("assistant"):

        if operazione == "insert":
            st.warning(
                f"⚠️ Stai per inserire una nuova riga nella tabella **{pw['table']}**. "
                "Controlla i campi prima di confermare."
            )

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
                        disabled=is_missing
                        and st.session_state.get(
                            f"null_{name}_{st.session_state.current_chat_id}",
                            True,
                        ),
                    )

                with col2:
                    if is_missing:
                        is_null = st.checkbox(
                            "NULL",
                            value=True,
                            key=f"null_{name}_{st.session_state.current_chat_id}",
                        )
                    else:
                        is_null = False

                valori_finali[name] = None if is_null else val

            c1, c2 = st.columns(2)

            if c1.button(
                "✅ Conferma ed esegui l'inserimento",
                use_container_width=True,
            ):
                try:
                    sql = execute_insert(pw["table"], valori_finali)
                    msg = (
                        f"Riga inserita correttamente nella tabella {pw['table']}.\n\n"
                        f"Query eseguita: `{sql}`"
                    )
                    current_messages.append({"role": "assistant", "content": msg})
                except Exception as e:
                    current_messages.append(
                        {
                            "role": "assistant",
                            "content": f"⚠️ Errore durante l'inserimento: {e}",
                        }
                    )

                st.session_state.pending_write = None
                st.rerun()

            if c2.button("❌ Annulla", use_container_width=True):
                current_messages.append(
                    {"role": "assistant", "content": "Operazione annullata."}
                )
                st.session_state.pending_write = None
                st.rerun()

        elif operazione == "update":
            st.warning(
                f"⚠️ Stai per modificare righe nella tabella **{pw['table']}**. "
                "Controlla campi e condizione prima di confermare."
            )

            st.markdown(
                "**Campi da modificare** "
                "(lascia vuoto e spunta NULL per azzerare un campo):"
            )

            set_finale = {}

            for name, value in pw["set"].items():
                col1, col2 = st.columns([3, 1])

                with col1:
                    is_null_default = value in (None, "", "NULL", "null")
                    val = st.text_input(
                        name.lower().replace("_", " "),
                        value="" if is_null_default else str(value),
                        key=f"set_{name}_{st.session_state.current_chat_id}",
                        disabled=st.session_state.get(
                            f"setnull_{name}_{st.session_state.current_chat_id}",
                            is_null_default,
                        ),
                    )

                with col2:
                    is_null = st.checkbox(
                        "NULL",
                        value=is_null_default,
                        key=f"setnull_{name}_{st.session_state.current_chat_id}",
                    )

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
                st.info(
                    "Non ho riconosciuto campi da modificare. "
                    "Riscrivi la richiesta specificando cosa cambiare."
                )

            if not pw["where"]:
                st.info(
                    "Non ho riconosciuto una condizione. "
                    "Riscrivi la richiesta specificando come identificare le righe."
                )

            elif all(where_finale.values()):
                try:
                    colonne_prev, righe_prev = fetch_matching_rows(
                        pw["table"],
                        where_finale,
                    )

                    if righe_prev:
                        st.markdown(
                            "**Riga/e attualmente nel database "
                            "(prima della modifica):**"
                        )
                        st.dataframe(
                            [dict(zip(colonne_prev, r)) for r in righe_prev],
                            use_container_width=True,
                        )
                    else:
                        st.warning(
                            "Nessuna riga corrisponde a questa condizione: "
                            "verifica i valori inseriti."
                        )

                except Exception as e:
                    st.error(f"Impossibile mostrare l'anteprima: {e}")

            c1, c2 = st.columns(2)

            if c1.button(
                "✅ Conferma ed esegui la modifica",
                use_container_width=True,
                disabled=not (
                    set_finale
                    and where_finale
                    and all(where_finale.values())
                ),
            ):
                try:
                    sql, righe = execute_update(
                        pw["table"],
                        set_finale,
                        where_finale,
                    )
                    msg = (
                        f"{righe} riga/e modificata/e nella tabella {pw['table']}.\n\n"
                        f"Query eseguita: `{sql}`"
                    )
                    current_messages.append({"role": "assistant", "content": msg})

                except Exception as e:
                    current_messages.append(
                        {
                            "role": "assistant",
                            "content": f"⚠️ Errore durante la modifica: {e}",
                        }
                    )

                st.session_state.pending_write = None
                st.rerun()

            if c2.button("❌ Annulla", use_container_width=True):
                current_messages.append(
                    {"role": "assistant", "content": "Operazione annullata."}
                )
                st.session_state.pending_write = None
                st.rerun()

        elif operazione == "delete":
            st.error(
                f"🗑️ Stai per **eliminare definitivamente** righe dalla tabella "
                f"**{pw['table']}**. Questa operazione non è reversibile."
            )

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
                st.info(
                    "Non ho riconosciuto una condizione. "
                    "Riscrivi la richiesta specificando come identificare le righe da eliminare."
                )

            else:
                try:
                    conteggio = count_matching(
                        pw["table"],
                        where_finale,
                    )
                    st.warning(
                        f"Questa condizione corrisponde a **{conteggio} riga/e**. "
                        "Verranno eliminate tutte."
                    )

                    if conteggio:
                        colonne_prev, righe_prev = fetch_matching_rows(
                            pw["table"],
                            where_finale,
                        )
                        st.markdown("**Riga/e che verranno eliminate:**")
                        st.dataframe(
                            [dict(zip(colonne_prev, r)) for r in righe_prev],
                            use_container_width=True,
                        )

                except Exception as e:
                    st.error(f"Impossibile verificare l'anteprima: {e}")

            conferma_esplicita = st.checkbox(
                "Confermo di voler eliminare definitivamente queste righe",
                key=f"confirm_delete_{st.session_state.current_chat_id}",
            )

            c1, c2 = st.columns(2)

            if c1.button(
                "🗑️ Elimina definitivamente",
                use_container_width=True,
                disabled=not (where_finale and conferma_esplicita),
            ):
                try:
                    sql, righe = execute_delete(
                        pw["table"],
                        where_finale,
                    )
                    msg = (
                        f"{righe} riga/e eliminata/e dalla tabella {pw['table']}.\n\n"
                        f"Query eseguita: `{sql}`"
                    )
                    current_messages.append({"role": "assistant", "content": msg})

                except Exception as e:
                    current_messages.append(
                        {
                            "role": "assistant",
                            "content": f"⚠️ Errore durante l'eliminazione: {e}",
                        }
                    )

                st.session_state.pending_write = None
                st.rerun()

            if c2.button("❌ Annulla", use_container_width=True):
                current_messages.append(
                    {"role": "assistant", "content": "Operazione annullata."}
                )
                st.session_state.pending_write = None
                st.rerun()


pending_active = bool(st.session_state.pending_write)


if st.session_state.get("pending_chart"):
    chart_request = st.session_state.pending_chart

    try:
        with st.chat_message("assistant"):
            mostra_risultato_query(
                chart_request["colonne"],
                chart_request["righe"],
                chart_request["query"],
                titolo="Risultato + rappresentazione grafica",
            )

    except Exception as e:
        with st.chat_message("assistant"):
            st.error(f"⚠️ Non sono riuscito a visualizzare il grafico: {e}")

    finally:
        st.session_state.pending_chart = None


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

            body {
                margin: 0;
                display: flex;
                justify-content: center;
                align-items: center;
            }
            </style>

            <button
                id="micButton"
                type="button"
                class="mic-btn"
                onclick="startDictation()"
                title="Parla con il microfono"
            >🎤</button>

            <script>
            function startDictation() {
                const btn = document.getElementById('micButton');

                if (window.hasOwnProperty('webkitSpeechRecognition')) {
                    var recognition = new webkitSpeechRecognition();
                    recognition.continuous = false;
                    recognition.interimResults = false;
                    recognition.lang = "it-IT";

                    recognition.onstart = function() {
                        if (btn) btn.classList.add('recording');
                    };

                    recognition.onend = function() {
                        if (btn) btn.classList.remove('recording');
                    };

                    recognition.onerror = function() {
                        if (btn) btn.classList.remove('recording');
                    };

                    recognition.onresult = function(e) {
                        var transcript = e.results[0][0].transcript;
                        var input = window.parent.document.querySelector(
                            'input[aria-label="Messaggio"]'
                        );

                        if (input) {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.parent.HTMLInputElement.prototype,
                                'value'
                            ).set;

                            setter.call(input, transcript);
                            input.dispatchEvent(
                                new Event('input', { bubbles: true })
                            );
                        }
                    };

                    recognition.start();

                } else {
                    alert(
                        'Il riconoscimento vocale non è supportato da questo browser.'
                    );
                }
            }
            </script>
            """,
            height=45,
        )

    with c_send:
        submit = st.form_submit_button(
            "Invia",
            type="primary",
            use_container_width=True,
            disabled=pending_active,
        )


if submit and user_msg.strip():
    current_messages.append(
        {"role": "user", "content": user_msg}
    )

    with st.spinner("Elaborazione della richiesta in corso..."):
        try:
            if _is_simple_message(user_msg):
                if user_msg.lower().strip() in {
                    "grazie",
                    "ok",
                    "okay",
                    "perfetto",
                    "va bene",
                }:
                    risposta = "Di nulla! Sono qui per aiutarti con il database."
                else:
                    risposta = "Ciao! Come posso aiutarti con il database Oracle?"

                current_messages.append(
                    {"role": "assistant", "content": risposta}
                )
                st.rerun()

            _consume_ai_request()

            tabelle = get_allowed_tables()
            analisi = analizza_richiesta(user_msg, tabelle)

            intento = analisi["intento"]
            tabella = analisi["tabella"]

            if intento == "altro":
                risposta = (
                    "Posso aiutarti a interrogare e gestire il database Oracle "
                    "usando il linguaggio naturale. Prova a chiedermi, ad esempio, "
                    "di cercare pazienti, contare record, confrontare dati o "
                    "visualizzare un grafico."
                )

                current_messages.append(
                    {"role": "assistant", "content": risposta}
                )

            elif intento == "grafico":
                query = analisi.get("sql")

                if not query:
                    raise ValueError(
                        "Non è stata generata una query SQL per il grafico."
                    )

                try:
                    colonne, righe = esegui_query_sicura_sola_lettura(query)

                except Exception as errore_sql:
                    testo_errore = str(errore_sql).upper()

                    # Correzione automatica per ORA-30076 e altri errori SQL
                    # tipicamente dovuti a tipo/funzione non compatibile.
                    errori_correggibili = (
                        "ORA-30076",
                        "ORA-00932",
                        "ORA-01858",
                        "ORA-01722",
                        "ORA-00904",
                    )

                    if any(codice in testo_errore for codice in errori_correggibili):
                        query = correggi_query_sql_se_necessario(
                            user_msg,
                            query,
                            errore_sql,
                            tabelle,
                        )

                        colonne, righe = esegui_query_sicura_sola_lettura(
                            query
                        )

                    else:
                        raise

                current_messages.append(
                    {
                        "role": "assistant",
                        "content": formatta_risultato_query(
                            colonne,
                            righe,
                            query,
                        ),
                    }
                )

                st.session_state.pending_chart = {
                    "colonne": colonne,
                    "righe": righe,
                    "query": query,
                }

            elif intento in ("insert", "update", "delete"):
                if not tabella or tabella.upper() not in [
                    t.upper() for t in tabelle
                ]:
                    current_messages.append(
                        {
                            "role": "assistant",
                            "content": (
                                "Non ho identificato una tabella valida per questa operazione. "
                                f"Tabelle disponibili: {', '.join(tabelle)}"
                            ),
                        }
                    )

                else:
                    tabella_real = [
                        t
                        for t in tabelle
                        if t.upper() == tabella.upper()
                    ][0]

                    if intento == "insert":
                        cols = get_table_columns(tabella_real)

                        st.session_state.pending_write = {
                            "operation": "insert",
                            "table": tabella_real,
                            "columns": cols,
                            "provided": {
                                str(k).upper(): v
                                for k, v in analisi["campi"].items()
                            },
                        }

                    elif intento == "update":
                        st.session_state.pending_write = {
                            "operation": "update",
                            "table": tabella_real,
                            "set": {
                                str(k).upper(): v
                                for k, v in analisi["campi"].items()
                            },
                            "where": {
                                str(k).upper(): v
                                for k, v in analisi["where"].items()
                            },
                        }

                    elif intento == "delete":
                        st.session_state.pending_write = {
                            "operation": "delete",
                            "table": tabella_real,
                            "where": {
                                str(k).upper(): v
                                for k, v in analisi["where"].items()
                            },
                        }

            else:  # read
                query = analisi.get("sql")

                if not query:
                    raise ValueError(
                        "Non è stata generata una query SQL per la richiesta."
                    )

                try:
                    colonne, righe = esegui_query_sicura_sola_lettura(query)

                except Exception as errore_sql:
                    testo_errore = str(errore_sql).upper()

                    errori_correggibili = (
                        "ORA-30076",
                        "ORA-00932",
                        "ORA-01858",
                        "ORA-01722",
                        "ORA-00904",
                    )

                    if any(codice in testo_errore for codice in errori_correggibili):
                        query = correggi_query_sql_se_necessario(
                            user_msg,
                            query,
                            errore_sql,
                            tabelle,
                        )

                        colonne, righe = esegui_query_sicura_sola_lettura(
                            query
                        )

                    else:
                        raise

                current_messages.append(
                    {
                        "role": "assistant",
                        "content": formatta_risultato_query(
                            colonne,
                            righe,
                            query,
                        ),
                    }
                )

                st.session_state.last_result = {
                    "colonne": colonne,
                    "righe": righe,
                    "query": query,
                    "grafico": False,
                }

        except Exception as e:
            error_text = str(e)

            if (
                "429" in error_text
                or "RESOURCE_EXHAUSTED" in error_text
                or "limite di richieste" in error_text.lower()
            ):
                risposta_errore = (
                    "⚠️ **Limite Gemini raggiunto.**\n\n"
                    "La quota API del progetto è temporaneamente esaurita. "
                    "La richiesta non verrà ritentata automaticamente, così da non consumare "
                    "ulteriormente la quota. Puoi riprovare quando il limite viene ripristinato "
                    "oppure aumentare il piano/quota del progetto Google AI."
                )

            else:
                risposta_errore = (
                    f"⚠️ Si è verificato un errore durante l'elaborazione: {e}"
                )

            current_messages.append(
                {
                    "role": "assistant",
                    "content": risposta_errore,
                }
            )

    st.rerun()