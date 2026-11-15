import os
import re
import time
import urllib
import requests
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from langchain_core.tools import tool
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore

# ==========================================
# 1. ENVIRONMENT & DYNAMIC INDEX ATTACHMENT
# ==========================================
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent  

load_dotenv(dotenv_path=project_root / ".env")

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")


def get_cached_huggingface_embeddings(model_name: str):
    """
    Loads and locks the HuggingFace model weights into the machine's global RAM.
    If called again during any subsequent script rerun, it returns instantly.
    """
    import streamlit as st
    
    # We wrap the inner call with st.cache_resource dynamically 
    @st.cache_resource(show_spinner=False)
    def _load_model(name: str):
        print(f"🧠 MEMORY SEED: Permanently caching local model [{name}] in global RAM...")
        from langchain_huggingface import HuggingFaceEmbeddings
        return HuggingFaceEmbeddings(
            model_name=name,
            model_kwargs={'device': 'cpu'}
        )
    return _load_model(model_name)

EMBEDDINGS_MODEL_SETTING = os.getenv("Embeddings_model", "LOCAL").strip().upper()

db_host = os.getenv("SQL_SERVER_HOST", "localhost")
db_port = os.getenv("SQL_SERVER_PORT", "1433")
db_user = os.getenv("SQL_AGENT_USER", "USR_FDE_RO")
db_password = os.getenv("SQL_AGENT_PASSWORD")

if not PINECONE_API_KEY:
    raise ValueError("CRITICAL: Ensure PINECONE_API_KEY is present in your active .env profile.")

if EMBEDDINGS_MODEL_SETTING == "OPENAI":
    print("🤖 Mode: Connecting to Cloud OpenAI Index (1536 Dim Space)...")
    embeddings = OpenAIEmbeddings()
    INDEX_NAME = "fde-sop-index-openai"
elif EMBEDDINGS_MODEL_SETTING == "VERTEX":
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION")
    vertex_model = os.getenv("Vertex_Embedding_Model", "text-embedding-004").strip()
    if not project or not location:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION must be set in .env"
        )
    print(f"Mode: Vertex AI embeddings [{vertex_model}] in {location} (768 dim)...")
    embeddings = GoogleGenerativeAIEmbeddings(
        model=vertex_model,
        project=project,
        location=location,
        vertexai=True,
    )
    INDEX_NAME = "fde-sop-index-vertex"
else :
    local_model_target = os.getenv("Local_Embedding_Model", "BAAI/bge-m3").strip()
    
    print(f"🤗 Mode: Connecting to Local Fallback [{local_model_target}] Index (1024 Dim Space)...")

    try:
        import streamlit as st
        if st.runtime.exists():
            embeddings = get_cached_huggingface_embeddings(local_model_target)
        else:
            from langchain_huggingface import HuggingFaceEmbeddings
            embeddings = HuggingFaceEmbeddings(model_name=local_model_target, model_kwargs={'device': 'cpu'})
    except ImportError:
        from langchain_huggingface import HuggingFaceEmbeddings
        embeddings = HuggingFaceEmbeddings(model_name=local_model_target, model_kwargs={'device': 'cpu'})

    INDEX_NAME = "fde-sop-index-local"

vector_store = PineconeVectorStore(index_name=INDEX_NAME, embedding=embeddings)
retriever = vector_store.as_retriever(search_kwargs={"k": 2})

# One dispatcher turn may call each tool twice. The third call is refused.
MAX_CALLS_PER_TURN = 2
SQL_TIMEOUT_SECONDS = 8
WEATHER_TIMEOUT_SECONDS = 6
SOP_TIMEOUT_SECONDS = 8
_WEATHER_RETRY_SECONDS = 0.4
_BREAKER_FAILURES = 3
_BREAKER_WINDOW_SECONDS = 60
_BREAKER_OPEN_SECONDS = 30

_turn_counts: ContextVar[dict | None] = ContextVar("fde_tool_turn", default=None)
_sop_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sop-search")
_SQL_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|EXEC|EXECUTE|MERGE|GRANT|REVOKE|CREATE|INTO)\b",
    re.IGNORECASE,
)


class ServiceBreaker:
    """Opens after repeated connection failures so the next chat does not wait on a dead service."""

    def __init__(self, name: str):
        self.name = name
        self._failures: deque[float] = deque()
        self._open_until = 0.0

    def allow(self) -> str | None:
        if time.monotonic() < self._open_until:
            return (
                f"error: {self.name} is unavailable after repeated failures. "
                "Continue without it. Do not invent its numbers."
            )
        return None

    def record_success(self) -> None:
        self._failures.clear()
        self._open_until = 0.0

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > _BREAKER_WINDOW_SECONDS:
            self._failures.popleft()
        if len(self._failures) >= _BREAKER_FAILURES:
            self._open_until = now + _BREAKER_OPEN_SECONDS

    def reset(self) -> None:
        self._failures.clear()
        self._open_until = 0.0


sql_breaker = ServiceBreaker("the shipment database")
weather_breaker = ServiceBreaker("the weather service")
sop_breaker = ServiceBreaker("the rule book")


def is_tool_failure(content: str) -> bool:
    lowered = (content or "").lower()
    return (
        lowered.startswith("error")
        or "database error" in lowered
        or "communication failure" in lowered
        or "retrieval error" in lowered
        or "security block" in lowered
        or "unavailable" in lowered
    )


def _is_outage(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "timeout",
            "timed out",
            "communication link",
            "connection",
            "network",
            "unavailable",
            "temporarily",
            "08001",
            "hyt00",
            "hyt01",
            "08s01",
        )
    )


@contextmanager
def tool_turn():
    """Count tool calls for the chat turn running on this thread."""
    token = _turn_counts.set({})
    try:
        yield
    finally:
        _turn_counts.reset(token)


def _over_turn_cap(tool_name: str) -> str | None:
    counts = _turn_counts.get()
    if counts is None:
        return None
    count = counts.get(tool_name, 0) + 1
    counts[tool_name] = count
    if count > MAX_CALLS_PER_TURN:
        return (
            f"error: {tool_name} already ran {MAX_CALLS_PER_TURN} times this turn. "
            "Stop calling it and answer from the results you have."
        )
    return None


def _validate_sql(sql_query: str) -> str | None:
    statement = sql_query.strip()
    if not statement.upper().startswith("SELECT"):
        return "error: SECURITY BLOCK: Only SELECT operations are authorized on this view."
    if ";" in statement:
        return "error: SECURITY BLOCK: Only one SELECT statement is authorized."
    if _SQL_FORBIDDEN.search(statement):
        return "error: SECURITY BLOCK: Only read-only SELECT operations are authorized."
    return None

# ==========================================
# 2. CORE FDE AGENT TOOLS
# ==========================================

@tool
def query_telemetry_db(sql_query: str) -> str:
    """
    Executes a SQL SELECT query against the FDE_VIEWS.VW_ACTIVE_FLEET view.
    Columns available:
    Timestamp, Latitude, Longitude, Current_Temperature_C, Cargo_Condition_Code,
    Risk_Classification, Delay_Probability, Port_Congestion_Level, Route_Risk_Index.
    Always write standard T-SQL queries.
    """
    blocked = _over_turn_cap("query_telemetry_db")
    if blocked:
        return blocked
    invalid = _validate_sql(sql_query)
    if invalid:
        return invalid
    closed = sql_breaker.allow()
    if closed:
        return closed

    connection_string = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={db_host},{db_port};"
            f"DATABASE=master;"
            f"UID={db_user};"
            f"PWD={db_password};"
            f"Encrypt=no;"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={SQL_TIMEOUT_SECONDS};"
        )

    params = urllib.parse.quote_plus(connection_string)
    engine = create_engine(f"mssql+pyodbc:///?odbc_connect={params}")

    try:
        with engine.connect() as conn:
            driver_conn = getattr(conn.connection, "driver_connection", None)
            if driver_conn is not None:
                driver_conn.timeout = SQL_TIMEOUT_SECONDS
            cursor = conn.execute(text(sql_query))
            columns = list(cursor.keys())
            rows = cursor.fetchmany(10)

            if not rows:
                sql_breaker.record_success()
                return "No records matched the query criteria."

            formatted_output = f"COLUMNS: {', '.join(columns)}\n"
            for row in rows:
                formatted_output += str(tuple(row)) + "\n"

            sql_breaker.record_success()
            return formatted_output
    except Exception as e:
        message = str(e)
        if _is_outage(message):
            sql_breaker.record_failure()
        return f"error: Database Error: {message}"
    finally:
        engine.dispose()

@tool
def fetch_corridor_conditions(latitude: float, longitude: float) -> str:
    """
    Fetches real-time weather and corridor conditions from a live REST API for given GPS coordinates.
    Provides temperature, wind speed, and computed corridor congestion index.
    """
    blocked = _over_turn_cap("fetch_corridor_conditions")
    if blocked:
        return blocked
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return (
            "error: Invalid coordinates. Latitude must be between -90 and 90 "
            "and longitude between -180 and 180."
        )
    closed = weather_breaker.allow()
    if closed:
        return closed

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}&current_weather=true"
    )
    last_error = "unknown weather failure"
    for attempt in (1, 2):
        try:
            response = requests.get(url, timeout=WEATHER_TIMEOUT_SECONDS)
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            payload = response.json().get("current_weather", {})
            temp = payload.get("temperature", "N/A")
            wind = payload.get("windspeed", 0.0)
            congestion_index = 8.5 if wind > 10.0 else 2.5
            status_note = "High Transit Disruption" if wind > 10.0 else "Corridor Normal"
            weather_breaker.record_success()
            return (
                f"--- LIVE CORRIDOR TELEMETRY ---\n"
                f"Target GPS: {latitude}, {longitude}\n"
                f"External Temp: {temp}°C | Wind Speed: {wind} km/h\n"
                f"Corridor Risk: {status_note} (Congestion Index: {congestion_index}/10)\n"
                f"-------------------------------"
            )
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else 0
            last_error = str(e)
            if status_code >= 500 and attempt == 1:
                time.sleep(_WEATHER_RETRY_SECONDS)
                continue
            if status_code >= 500:
                weather_breaker.record_failure()
            return f"error: Corridor API Communication Failure: {last_error}"
        except (requests.Timeout, requests.ConnectionError) as e:
            last_error = str(e)
            if attempt == 1:
                time.sleep(_WEATHER_RETRY_SECONDS)
                continue
            weather_breaker.record_failure()
        except Exception as e:
            return f"error: Corridor API Communication Failure: {e}"
    return f"error: Corridor API Communication Failure: {last_error}"

@tool
def search_compliance_sop(query: str) -> str:
    """
    Searches enterprise Standard Operating Procedures (SOPs) indexed in the Pinecone Vector DB.
    Use this to retrieve regulatory thresholds, cold-chain breach mitigations, and rerouting rules.
    """
    blocked = _over_turn_cap("search_compliance_sop")
    if blocked:
        return blocked
    cleaned = query.strip()
    if not cleaned:
        return "error: The rule-book search needs a question."
    if len(cleaned) > 500:
        return "error: The rule-book search is too long. Ask about one rule."
    closed = sop_breaker.allow()
    if closed:
        return closed

    future = _sop_pool.submit(retriever.invoke, cleaned)
    try:
        matched_docs = future.result(timeout=SOP_TIMEOUT_SECONDS)
    except FuturesTimeout:
        sop_breaker.record_failure()
        return "error: Vector Store Retrieval Error: the rule book did not answer within 8 seconds."
    except Exception as e:
        message = str(e)
        if _is_outage(message):
            sop_breaker.record_failure()
        return f"error: Vector Store Retrieval Error: {message}"

    if not matched_docs:
        sop_breaker.record_success()
        return "No matching compliance clauses found."

    formatted_context = "\n\n".join(
        [f"[Source: {doc.metadata.get('source_file', 'SOP')} | Format: {doc.metadata.get('file_format', 'RAW')}]\n{doc.page_content}" for doc in matched_docs]
    )
    sop_breaker.record_success()
    return f"--- COMPLIANCE SOP CONTEXT ---\n{formatted_context}\n------------------------------"

# ==========================================
# 3. LOCAL VERIFICATION
# ==========================================
if __name__ == "__main__":
    print("\n--- Testing Tool 1: SQL Telemetry View ---")
    print(query_telemetry_db.invoke("SELECT TOP 2 Latitude, Longitude, Current_Temperature_C FROM FDE_VIEWS.VW_ACTIVE_FLEET"))
    
    print("\n--- Testing Tool 2: Live Corridor API ---")
    print(fetch_corridor_conditions.invoke({"latitude": 33.77, "longitude": -118.19}))
    
    print("\n--- Testing Tool 3: Pinecone Vector Retrieval ---")
    print(search_compliance_sop.invoke("What are the temperature rules for fresh perishables?"))
