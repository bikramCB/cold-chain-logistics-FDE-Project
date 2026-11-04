import ast
import os
import sys
import uuid
import json
import html
import re
import urllib
from pathlib import Path
from dotenv import load_dotenv
import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from langchain_core.messages import HumanMessage, ToolMessage

# ==========================================
# 1. IMMEDIATE PATH & ENVIRONMENT RESOLUTION
# ==========================================
script_dir = Path(__file__).resolve().parent  # points to src/
project_root = script_dir.parent              # climbs to project root

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

load_dotenv(project_root / ".env")

# Import the compiled graph and tools list dynamically
from src.orchestrator import fde_agent
from src.guardrails import FALLBACK_ANSWER, check_answer, correction_message, screen_input
from src.sop_cache import is_sop_only, sop_cache

# ==========================================
# 2. SQL CREDENTIALS MAPPING FROM .ENV
# ==========================================
db_host = os.getenv("SQL_SERVER_HOST", "localhost")
db_port = os.getenv("SQL_SERVER_PORT", "1433")
db_user = os.getenv("SQL_AGENT_USER", "USR_FDE_RO")
db_password = os.getenv("SQL_AGENT_PASSWORD")

# Engine for the Agent to write logs using its standard credentials
connection_string = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={db_host},{db_port};"
    f"DATABASE=master;"
    f"UID={db_user};"
    f"PWD={db_password};"
    f"Encrypt=no;"
    f"TrustServerCertificate=yes;"
)

log_params = urllib.parse.quote_plus(connection_string)

log_engine = create_engine(f"mssql+pyodbc:///?odbc_connect={log_params}")

def write_audit_log(session_id, node_name, tool_name, content):
    """Silently writes agent execution traces to the SQL audit table using agent permissions."""
    try:
        with log_engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO FDE_VIEWS.AgentAuditLog (SessionID, NodeExecuted, ToolName, Content)
                VALUES (:session_id, :node_name, :tool_name, :content)
            """), {
                "session_id": session_id,
                "node_name": node_name,
                "tool_name": tool_name,
                "content": content
            })
            conn.commit()
    except Exception as e:
        print(f"Audit Log Failed (Silent): {e}")

# ==========================================
# 3. PAGE CONFIGURATION & ENTERPRISE THEME
# ==========================================
st.set_page_config(
    page_title="FDE Supply Chain Dispatch Console",
    page_icon="🧊",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stApp { background-color: #0B0E14; color: #E2E8F0; }
    div[data-testid="stSidebar"] { background-color: #111622; border-right: 1px solid #1E293B; }
    .stMarkdown code { background-color: #1E293B !important; color: #38BDF8 !important; }
    [data-testid="stChatMessage"] { background: transparent !important; }
    [data-testid="stChatInput"] textarea,
    [data-testid="stChatInputTextArea"] {
        color: #0F172A !important;
        -webkit-text-fill-color: #0F172A !important;
        background-color: #FFFFFF !important;
        caret-color: #0F172A !important;
        font-size: 1.05rem !important;
        line-height: 1.45 !important;
    }
    [data-testid="stChatInput"] textarea::placeholder,
    [data-testid="stChatInputTextArea"]::placeholder {
        color: #64748B !important;
        -webkit-text-fill-color: #64748B !important;
        opacity: 1 !important;
    }
    .welcome-card, .q-card, .a-card, .brief-banner {
        border-radius: 12px;
        padding: 14px 16px;
        margin: 8px 0 14px 0;
    }
    .welcome-card { background: #111827; border: 1px solid #334155; }
    .welcome-card h3 { margin: 0 0 6px 0; color: #F8FAFC !important; font-size: 1.2rem; }
    .welcome-card p { margin: 0; color: #E2E8F0 !important; font-size: 1.02rem; line-height: 1.5; }
    .q-card, .a-card {
        background: #FFFFFF !important;
        color: #0F172A !important;
        border: 1px solid #CBD5E1;
    }
    .q-card { border-left: 4px solid #1D4ED8; }
    .a-card { border-left: 4px solid #0F172A; }
    .q-card p, .a-card p, .a-card li, .a-card h3, .a-card strong, .a-card em {
        color: #0F172A !important;
        -webkit-text-fill-color: #0F172A !important;
    }
    .q-card p, .a-card p, .a-card li { margin: 0 0 8px 0; font-size: 1.05rem; line-height: 1.5; }
    .q-label, .a-label {
        color: #1D4ED8 !important;
        -webkit-text-fill-color: #1D4ED8 !important;
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        margin-bottom: 6px;
    }
    .brief-banner h3 { margin: 0 0 6px 0; font-size: 1.2rem; color: #0F172A !important; -webkit-text-fill-color: #0F172A !important; }
    .brief-banner p { margin: 0; font-size: 1.02rem; line-height: 1.5; color: #0F172A !important; -webkit-text-fill-color: #0F172A !important; }
    .brief-banner.bad { background: #FEF2F2; border: 1px solid #F87171; }
    .brief-banner.ok { background: #F0FDF4; border: 1px solid #34D399; }
    .brief-banner.info { background: #EFF6FF; border: 1px solid #93C5FD; }
    .a-card .section-label { color: #1D4ED8 !important; -webkit-text-fill-color: #1D4ED8 !important; font-size: 1.05rem; font-weight: 700; margin: 14px 0 6px 0; }
    .a-card ul, .a-card ol { margin: 0 0 8px 1.2rem; padding: 0; }
    .reason-box { background: #F8FAFC; border-left: 4px solid #1D4ED8; border-radius: 8px; padding: 10px 12px; margin: 8px 0 4px 0; }
    .fact-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 8px 0 12px 0; }
    .a-card .fact-card { background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px; padding: 12px 14px; }
    .a-card .fact-card .fact-k { color: #1D4ED8 !important; -webkit-text-fill-color: #1D4ED8 !important; font-size: 0.78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
    .a-card .fact-card .fact-v { color: #0F172A !important; -webkit-text-fill-color: #0F172A !important; font-size: 1.05rem; margin-top: 4px; line-height: 1.35; }
    [data-testid="stBottomBlockContainer"],
    [data-testid="stMain"],
    [data-testid="stAppViewContainer"] { background-color: #0B0E14; }
</style>
""", unsafe_allow_html=True)

EXAMPLE_QUESTIONS = [
    (
        "Check Los Angeles shipments",
        "Find any active shipments near Los Angeles (Latitude ~33.8, Longitude ~-118.1). "
        "Check the local weather there, and tell me if the current cargo temperature violates the SOP for fresh perishables.",
    ),
    (
        "Explain Tier 1 and Tier 2",
        "I'm a new dispatcher on the night shift. Can you quickly explain the difference between a Tier 1 and Tier 2 escalation?",
    ),
]

TOOL_PROGRESS = {
    "query_telemetry_db": "Looking up shipments in that area...",
    "fetch_corridor_conditions": "Checking the weather and traffic on that route...",
    "search_compliance_sop": "Checking the company rule book...",
}
TOOL_DONE = {
    "query_telemetry_db": "Shipment records",
    "fetch_corridor_conditions": "Weather and traffic",
    "search_compliance_sop": "Company rule book",
    "sop_cache": "Saved policy answer",
}
HEADING_LABELS = {
    "executive summary": "What this means",
    "what this means": "What this means",
    "telemetry & environment analysis": "What we found",
    "telemetry and environment analysis": "What we found",
    "what we found": "What we found",
    "required action plan": "What to do next",
    "what to do next": "What to do next",
}


def as_text(content) -> str:
    """Turn a model message into plain text. Gemini sometimes returns a list of parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def verdict_for(text: str):
    low = text.lower()
    if any(phrase in low for phrase in (
        "does not violate", "no violation", "within the", "within range",
        "is safe", "are safe", "compliant", "acceptable", "no breach",
    )):
        return ("All clear", "The cargo is inside the allowed range.", "ok")
    if any(phrase in low for phrase in (
        "violate", "violation", "too warm", "too hot", "exceed", "breach",
        "out of range", "above the", "not compliant",
    )):
        return ("Action needed", "The cargo is outside the cold-chain rule. Follow the steps below.", "bad")
    if any(phrase in low for phrase in (
        "no active", "no shipment", "none were found", "could not find",
        "no trucks", "no matching", "not found",
    )):
        return ("Nothing to act on", "No shipment matched this question.", "info")
    return ("Briefing", "Here is the answer in plain language.", "info")


def friendly_heading(title: str) -> str:
    cleaned = re.sub(r"^\d+\.\s*", "", title).strip().strip("*")
    return HEADING_LABELS.get(cleaned.lower(), cleaned)


def split_sections(text: str):
    preamble = []
    sections = []
    for part in re.split(r"\n(?=#{1,3}\s)", text.strip()):
        match = re.match(r"#{1,3}\s+(.+?)\s*\n(.*)", part, re.S)
        if match:
            sections.append((match.group(1).strip(), match.group(2).strip()))
        elif part.startswith("#"):
            sections.append((re.sub(r"^#{1,3}\s+", "", part).strip(), ""))
        elif part.strip():
            preamble.append(part.strip())
    return "\n\n".join(preamble), sections


def render_user_question(text: str):
    safe = html.escape(text).replace("\n", "<br>")
    st.markdown(
        f'<div class="q-card"><div class="q-label">Your question</div><p>{safe}</p></div>',
        unsafe_allow_html=True,
    )


_REASON_LINE = re.compile(r"^\*?(In short|Rule used)\*?\s*:?\s*(.+)$", re.I)


def _inline(text: str) -> str:
    safe = html.escape(text.strip())
    safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"\*(.+?)\*", r"<em>\1</em>", safe)
    return safe


def _block_html(text: str) -> str:
    parts = []
    items = []
    kind = None

    def flush():
        nonlocal items, kind
        if not items:
            return
        tag = "ol" if kind == "ol" else "ul"
        parts.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + f"</{tag}>")
        items = []
        kind = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        bullet = re.match(r"^[-*]\s+(.*)", stripped)
        number = re.match(r"^\d+\.\s+(.*)", stripped)
        if bullet:
            if kind not in (None, "ul"):
                flush()
            kind = "ul"
            items.append(bullet.group(1))
        elif number:
            if kind not in (None, "ol"):
                flush()
            kind = "ol"
            items.append(number.group(1))
        else:
            flush()
            parts.append(f"<p>{_inline(stripped)}</p>")
    flush()
    return "".join(parts)


def _extract_reasons(text: str):
    reasons = []
    kept = []
    for line in text.splitlines():
        match = _REASON_LINE.match(line.strip())
        if match:
            label = "In short" if match.group(1).lower().startswith("in short") else "Rule used"
            reasons.append((label, match.group(2).strip(" *")))
        else:
            kept.append(line)
    return "\n".join(kept).strip(), reasons


def strip_tables(text: str) -> str:
    """Drop markdown tables. The model often leaves their cells blank on screen."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") or re.match(r"^\|?\s*:?-{3,}", stripped):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _cargo_words(temp_text: str) -> str:
    try:
        temp = float(temp_text)
    except (TypeError, ValueError):
        return str(temp_text)
    if 0.0 <= temp <= 4.0:
        return f"{temp:g}°C, fine for fresh food"
    if temp > 4.0:
        return f"{temp:g}°C, too warm for fresh food"
    return f"{temp:g}°C, colder than the fresh-food range"


def facts_from_traces(traces) -> list[tuple[str, str]]:
    facts = []
    cargo_temp = None
    where = None
    condition = None
    for trace in traces or []:
        if trace.get("type") != "tool_output":
            continue
        content = as_text(trace.get("content"))
        if trace.get("name") == "query_telemetry_db":
            columns = []
            for line in content.splitlines():
                if line.startswith("COLUMNS:"):
                    columns = [part.strip() for part in line.split(":", 1)[1].split(",")]
                    continue
                if not line.startswith("("):
                    continue
                cleaned = re.sub(r"datetime\.datetime\([^)]*\)", "'time'", line)
                cleaned = re.sub(r"Decimal\('([^']*)'\)", r"\1", cleaned)
                try:
                    values = ast.literal_eval(cleaned)
                except (SyntaxError, ValueError):
                    continue
                if not columns or len(values) < len(columns):
                    continue
                record = dict(zip(columns, values[: len(columns)]))
                cargo_temp = record.get("Current_Temperature_C", cargo_temp)
                condition = record.get("Cargo_Condition_Code", condition)
                lat = record.get("Latitude")
                lon = record.get("Longitude")
                if lat is not None and lon is not None:
                    where = f"Near {lat}, {lon}"
        elif trace.get("name") == "fetch_corridor_conditions":
            outside = re.search(r"External Temp:\s*([-\d.]+)", content)
            wind = re.search(r"Wind Speed:\s*([-\d.]+)", content)
            risk = re.search(r"Corridor Risk:\s*([^(]+)", content)
            if outside:
                facts.append(("Outside temperature", f"{outside.group(1)}°C"))
            if wind:
                facts.append(("Wind", f"{wind.group(1)} km/h"))
            if risk:
                facts.append(("Road conditions", risk.group(1).strip()))
    ordered = []
    if where:
        ordered.append(("Where", where))
    if cargo_temp is not None:
        ordered.append(("Cargo temperature", _cargo_words(cargo_temp)))
    if condition is not None:
        ordered.append(("Cargo condition", str(condition)))
    ordered.extend(facts)
    return ordered


def _facts_html(traces) -> str:
    facts = facts_from_traces(traces)
    if not facts:
        return ""
    cards = []
    for label, value in facts:
        cards.append(
            "<div class='fact-card'>"
            f"<div class='fact-k'>{html.escape(str(label))}</div>"
            f"<div class='fact-v'>{html.escape(str(value))}</div>"
            "</div>"
        )
    return f"<div class='fact-grid'>{''.join(cards)}</div>"


def _reasons_html(reasons) -> str:
    if not reasons:
        return ""
    lines = "".join(
        f"<p><strong>{html.escape(label)}.</strong> {_inline(detail)}</p>"
        for label, detail in reasons
    )
    return f"<div class='reason-box'><div class='section-label'>Why</div>{lines}</div>"


def render_assistant_answer(content, traces=None):
    text = strip_tables(as_text(content).strip())
    if not text:
        st.warning("The assistant did not return an answer. Try the question again.")
        return
    text, reasons = _extract_reasons(text)
    title, subtitle, kind = verdict_for(text)
    facts_html = _facts_html(traces)
    preamble, sections = split_sections(text)
    blocks = [
        f'<div class="brief-banner {kind}"><h3>{html.escape(title)}</h3><p>{html.escape(subtitle)}</p></div>',
        _reasons_html(reasons),
    ]
    if sections:
        if preamble:
            blocks.append(_block_html(preamble))
        for raw_title, body in sections:
            heading = friendly_heading(raw_title)
            if heading == "What we found" and facts_html:
                body = re.sub(
                    r"(?m)^- (Where|Cargo temperature|Cargo condition|Weather and traffic):.*\n?",
                    "",
                    body,
                ).strip()
            body_html = _block_html(body) if body else ""
            if heading == "What we found":
                if not facts_html and not body_html:
                    continue
                blocks.append(f"<div class='section-label'>{html.escape(heading)}</div>{facts_html}{body_html}")
            else:
                blocks.append(
                    f"<div class='section-label'>{html.escape(heading)}</div>"
                    f"{body_html or '<p>No details in this section.</p>'}"
                )
    else:
        if facts_html:
            blocks.append(f"<div class='section-label'>What we found</div>{facts_html}")
        blocks.append(_block_html(text))
    st.markdown(
        f"<div class='a-card'><div class='a-label'>Answer</div>{''.join(blocks)}</div>",
        unsafe_allow_html=True,
    )


def render_traces(traces):
    if not traces:
        return
    with st.expander("Show the checks behind this answer", expanded=False):
        st.caption("This is the technical record. You do not need it to follow the answer above.")
        for trace in traces:
            done = TOOL_DONE.get(trace["name"], trace["name"])
            if trace["type"] == "tool_input":
                st.markdown(f"**{done}** — what was looked up")
                st.json(trace["args"])
            else:
                st.markdown(f"**{done}** — what came back")
                st.code(trace["content"], language="text")

def run_agent_turn(user_text: str, status) -> tuple[str, list]:
    """Stream one orchestrator turn. Returns the drafted answer and its traces."""
    final_response = ""
    current_traces = []
    events = fde_agent.stream(
        {"messages": [HumanMessage(content=user_text)]},
        config=thread_config,
        stream_mode="updates",
    )
    for event in events:
        for node_name, node_state in event.items():
            if node_name == "reasoner":
                latest_msg = node_state["messages"][-1]
                if hasattr(latest_msg, "tool_calls") and latest_msg.tool_calls:
                    for tool_call in latest_msg.tool_calls:
                        progress = TOOL_PROGRESS.get(tool_call["name"], "Checking one more source...")
                        status.update(label=progress)
                        status.write(progress)
                        current_traces.append({
                            "type": "tool_input",
                            "name": tool_call["name"],
                            "args": tool_call["args"],
                        })
                        write_audit_log(
                            session_id=st.session_state.thread_id,
                            node_name="reasoner",
                            tool_name=tool_call["name"],
                            content=json.dumps(tool_call["args"]),
                        )
                answer_text = as_text(latest_msg.content).strip()
                if answer_text:
                    final_response = answer_text
                    status.update(label="Writing the answer in plain language...")
                    write_audit_log(
                        session_id=st.session_state.thread_id,
                        node_name="reasoner_final",
                        tool_name="LLM Text Synthesis",
                        content=final_response,
                    )
            elif node_name == "tools":
                for msg in node_state.get("messages", []):
                    if isinstance(msg, ToolMessage):
                        progress = TOOL_PROGRESS.get(msg.name, "Checking one more source...")
                        status.update(label=progress)
                        status.write(TOOL_DONE.get(msg.name, msg.name) + " checked.")
                        current_traces.append({
                            "type": "tool_output",
                            "name": msg.name,
                            "content": msg.content,
                        })
                        write_audit_log(
                            session_id=st.session_state.thread_id,
                            node_name="tools",
                            tool_name=msg.name,
                            content=as_text(msg.content),
                        )
    return final_response, current_traces


def _cache_caption() -> str:
    idle_hours = sop_cache.idle_seconds / 3600
    max_hours = sop_cache.max_age_seconds / 3600
    return (
        f"Saved policy answer for this session. "
        f"It is deleted after {idle_hours:g} hours without a question, "
        f"and it never lasts longer than {max_hours:g} hours."
    )


def show_guardrail_message(message: str):
    st.markdown(
        f"<div class='a-card'><div class='a-label'>Answer</div><p>{html.escape(message)}</p></div>",
        unsafe_allow_html=True,
    )


# ==========================================
# 4. MULTI-USER STATE & THREAD MANAGEMENT
# ==========================================
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "ui_messages" not in st.session_state:
    st.session_state.ui_messages = []

thread_config = {"configurable": {"thread_id": st.session_state.thread_id}}

# ==========================================
# 5. SIDEBAR NAVIGATION & METADATA
# ==========================================
with st.sidebar:
    st.image(str(script_dir / "image_L25X5q.png") if (script_dir / "image_L25X5q.png").exists() else "https://cdn-icons-png.flaticon.com/512/2830/2830305.png", width=65)
    st.title("FDE Command Center")
    
    app_mode = st.radio("System Mode", ["🧊 Dispatch Console", "🛡️ Security & Audit Logs"])
    
    st.markdown("---")
    st.caption(f"Session Token: `{st.session_state.thread_id[:8]}...`")
    st.markdown(f"**Reasoning Architecture:** `{os.getenv('Agent_llm', 'DEEPSEEK')}`")
    
    st.markdown("---")
    if st.button("🗑️ Purge Dispatch Workspace Session", use_container_width=True):
        sop_cache.drop_session(st.session_state.thread_id)
        st.session_state.ui_messages = []
        st.session_state.thread_id = str(uuid.uuid4())
        st.rerun()

# ==========================================
# 6. VIEW ROUTING (DISPATCH VS AUDIT)
# ==========================================

if app_mode == "🧊 Dispatch Console":
    # ------------------------------------------
    # TAB 1: CHAT UI & AGENT EXECUTION
    # ------------------------------------------
    st.title("Cold-Chain Incident Control Dashboard")
    st.caption("Ask in everyday words. The answer says what is wrong, what was found, and what to do next.")

    if not st.session_state.ui_messages:
        st.markdown(
            '<div class="welcome-card"><h3>Start with a question</h3>'
            "<p>Type it in the box at the bottom, or use one of the buttons. "
            "Your question will show up as a card, and the answer will be written for a dispatcher, not an engineer.</p></div>",
            unsafe_allow_html=True,
        )
        starter_cols = st.columns(2)
        for column, (label, question) in zip(starter_cols, EXAMPLE_QUESTIONS):
            with column:
                if st.button(label, use_container_width=True):
                    st.session_state.pending_question = question
                    st.rerun()

    for entry in st.session_state.ui_messages:
        with st.chat_message(entry["role"], avatar="👤" if entry["role"] == "user" else "🤖"):
            if entry["role"] == "user":
                render_user_question(entry["content"])
            else:
                render_assistant_answer(entry["content"], entry.get("traces") or [])
                if entry.get("cached"):
                    st.caption(_cache_caption())
                render_traces(entry.get("traces") or [])

    typed_question = st.chat_input("Ask about a shipment, the weather, or a company rule...")
    if typed_question:
        user_input = typed_question
    else:
        user_input = st.session_state.pop("pending_question", None)

    if user_input:
        decision = screen_input(user_input, has_history=bool(st.session_state.ui_messages))
        st.session_state.ui_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user", avatar="👤"):
            render_user_question(user_input)

        with st.chat_message("assistant", avatar="🤖"):
            if not decision.allowed:
                write_audit_log(
                    session_id=st.session_state.thread_id,
                    node_name="input_guard",
                    tool_name=decision.code,
                    content=user_input,
                )
                show_guardrail_message(decision.message)
                st.session_state.ui_messages.append({
                    "role": "assistant",
                    "content": decision.message,
                    "traces": [],
                })
            else:
                sop_cache.begin_turn(st.session_state.thread_id)
                cached = sop_cache.get(st.session_state.thread_id, user_input)
                if cached:
                    cache_traces = [{
                        "type": "tool_output",
                        "name": "sop_cache",
                        "content": "Reused the policy answer saved for this session. Shipment and weather lookups were not reused.",
                    }]
                    write_audit_log(
                        session_id=st.session_state.thread_id,
                        node_name="sop_cache",
                        tool_name="sop_cache",
                        content=cached.answer,
                    )
                    render_assistant_answer(cached.answer, cache_traces)
                    st.caption(_cache_caption())
                    render_traces(cache_traces)
                    st.session_state.ui_messages.append({
                        "role": "assistant",
                        "content": cached.answer,
                        "traces": cache_traces,
                        "cached": True,
                    })
                else:
                    final_response = ""
                    current_traces = []
                    with st.status("Reading your question...", expanded=True) as status:
                        final_response, current_traces = run_agent_turn(user_input, status)
                        issues = check_answer(final_response, current_traces)
                        if issues:
                            status.update(label="The draft disagreed with the records. Checking once more...")
                            write_audit_log(
                                session_id=st.session_state.thread_id,
                                node_name="output_guard",
                                tool_name="retry",
                                content="\n".join(issues),
                            )
                            retried, retry_traces = run_agent_turn(correction_message(issues), status)
                            combined = current_traces + retry_traces
                            if retried and not check_answer(retried, combined):
                                final_response = retried
                                current_traces = combined
                            else:
                                final_response = ""
                        status.update(label="Answer ready", state="complete", expanded=False)

                    if final_response and is_sop_only(current_traces):
                        sop_cache.put(st.session_state.thread_id, user_input, final_response)

                    if final_response:
                        render_assistant_answer(final_response, current_traces)
                        render_traces(current_traces)
                        st.session_state.ui_messages.append({
                            "role": "assistant",
                            "content": final_response,
                            "traces": current_traces,
                        })
                    elif issues:
                        write_audit_log(
                            session_id=st.session_state.thread_id,
                            node_name="output_guard",
                            tool_name="blocked",
                            content=FALLBACK_ANSWER,
                        )
                        show_guardrail_message(FALLBACK_ANSWER)
                        render_traces(current_traces)
                        st.session_state.ui_messages.append({
                            "role": "assistant",
                            "content": FALLBACK_ANSWER,
                            "traces": current_traces,
                        })
                    else:
                        st.error("The assistant could not finish an answer. Check that the database is running, then ask again.")


elif app_mode == "🛡️ Security & Audit Logs":
    # ------------------------------------------
    # TAB 2: AUDIT LOG VIEWER (REQUIRES ADMIN CREDENTIALS FROM .ENV OR INPUT)
    # ------------------------------------------
    st.title("🛡️ Enterprise Agent Audit Trail")
    st.caption("Secure database inspection of FDE_VIEWS.AgentAuditLog")
    
    st.markdown("### Database Authorization Gate")
    st.markdown("Enter high-privilege administrative credentials (defined in `.env` as `SQL_ADMIN_USER`) to query audit logs.")
    
    with st.form("admin_auth_form"):
        col1, col2 = st.columns(2)
        with col1:
            input_user = st.text_input("Admin Username", value=os.getenv("SQL_ADMIN_USER", ""))
        with col2:
            input_pass = st.text_input("Admin Password", type="password", value="")
            
        submit_admin = st.form_submit_button("Authenticate & Load Logs", use_container_width=True)

    if submit_admin:
        expected_admin_user = os.getenv("SQL_ADMIN_USER")
        expected_admin_pass = os.getenv("SQL_ADMIN_PASSWORD")
        
        if input_user == expected_admin_user and input_pass == expected_admin_pass:
            try:
                # Build an isolated admin connection string for viewing data
                admin_params = urllib.parse.quote_plus(
                    "DRIVER={ODBC Driver 18 for SQL Server};"
                    f"SERVER={db_host},{db_port};"
                    "DATABASE=master;"
                    f"UID={input_user};"
                    f"PWD={input_pass};"
                    "Encrypt=no;"
                    "TrustServerCertificate=yes;"
                )
                admin_engine = create_engine(f"mssql+pyodbc:///?odbc_connect={admin_params}")
                
                with admin_engine.connect() as conn:
                    query = """
                        SELECT LogID, Timestamp, SessionID, NodeExecuted, ToolName, Content 
                        FROM FDE_VIEWS.AgentAuditLog 
                        ORDER BY Timestamp DESC
                    """
                    df = pd.read_sql(query, conn)
                
                st.success("✅ Authenticated successfully as Admin.")
                
                if not df.empty:
                    st.dataframe(
                        df,
                        column_config={
                            "LogID": st.column_config.NumberColumn("ID", format="%d"),
                            "Timestamp": st.column_config.DatetimeColumn("Execution Time", format="DD/MM/YYYY-h:mm a"),
                            "SessionID": "Session Token",
                            "NodeExecuted": "Graph Node",
                            "ToolName": "Tool Triggered",
                            "Content": "Raw Payload Data"
                        },
                        hide_index=True,
                        use_container_width=True,
                        height=600
                    )
                else:
                    st.info("No audit logs found in the database. Run a query in the Dispatch Console first.")
                    
            except Exception as e:
                st.error(f"Database Query Failed: {e}")
        else:
            st.error("❌ Invalid Administrator Credentials.")