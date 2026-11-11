"""Grade the last 24 hours of dispatcher answers and store the scores.

One BigQuery row is one final answer, together with the tool rows, guardrail
rows, token counts, and cache age that were written for that same turn.
"""
import json
import os
import urllib.parse
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery
from langchain_google_genai import ChatGoogleGenerativeAI
from sqlalchemy import create_engine, text

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ["GOOGLE_CLOUD_LOCATION"]
TABLE = f"{PROJECT}.eval.judge_scores"
MAX_CACHE_AGE = int(os.getenv("SOP_CACHE_MAX_AGE_SECONDS", "28800"))
INPUT_USD_PER_MILLION = float(os.getenv("GEMINI_INPUT_USD_PER_MILLION", "0"))
OUTPUT_USD_PER_MILLION = float(os.getenv("GEMINI_OUTPUT_USD_PER_MILLION", "0"))


def message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return str(content)


def parse_scores(raw):
    cleaned = message_text(raw).strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"Judge did not return JSON: {cleaned[:500]}")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Judge JSON could not be read: {cleaned[:500]}") from exc
    return {
        "tone": int(data["tone"]),
        "actionability": int(data["actionability"]),
        "safety": int(data["safety"]),
        "groundedness": int(data["groundedness"]),
        "comments": str(data.get("comments", ""))[:2000],
    }


def fetch_rows():
    host = os.environ["SQL_SERVER_HOST"]
    port = os.getenv("SQL_SERVER_PORT", "1433")
    user = os.environ["SQL_EVAL_USER"]
    password = os.environ["SQL_EVAL_PASSWORD"]
    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={host},{port};"
        "DATABASE=master;"
        f"UID={user};"
        f"PWD={password};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )
    params = urllib.parse.quote_plus(connection_string)
    engine = create_engine(f"mssql+pyodbc:///?odbc_connect={params}")
    query = text(
        """
        SELECT SessionID, Timestamp, NodeExecuted, ToolName, Content
        FROM FDE_VIEWS.AgentAuditLog
        WHERE Timestamp >= DATEADD(hour, -24, GETDATE())
        ORDER BY SessionID, Timestamp, LogID
        """
    )
    with engine.connect() as conn:
        return pd.read_sql(query, conn)


def _text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def _json_dict(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _tool_failed(content: str) -> bool:
    lowered = content.lower()
    return lowered.startswith("error") or "retrieval error" in lowered


def split_turns(frame: pd.DataFrame) -> list[list[dict]]:
    """A turn ends at each reasoner_final row. Earlier rows in that session belong to it."""
    turns = []
    current = []
    for _, row in frame.iterrows():
        item = {
            "timestamp": row["Timestamp"],
            "node": _text(row["NodeExecuted"]),
            "tool": _text(row["ToolName"]),
            "content": _text(row["Content"]),
        }
        current.append(item)
        if item["node"] == "reasoner_final":
            turns.append(current)
            current = []
    return turns


def summarize_turn(turn: list[dict]) -> dict:
    final = next(item for item in reversed(turn) if item["node"] == "reasoner_final")
    sop_parts = [
        item["content"]
        for item in turn
        if item["node"] == "tools" and item["tool"] == "search_compliance_sop" and item["content"]
    ]
    tool_lines = []
    meta_tools = set()
    failures = {}
    duration_ms = 0
    for item in turn:
        if item["node"] == "tool_meta":
            meta = _json_dict(item["content"])
            meta_tools.add(item["tool"])
            duration_ms += int(meta.get("duration_ms") or 0)
            if meta.get("status") == "error":
                failures[item["tool"]] = failures.get(item["tool"], 0) + 1
        elif item["node"] == "tools":
            tool_lines.append(f"{item['tool']}: {item['content'][:500]}")
    for item in turn:
        if item["node"] == "tools" and item["tool"] not in meta_tools and _tool_failed(item["content"]):
            failures[item["tool"]] = failures.get(item["tool"], 0) + 1

    input_tokens = 0
    output_tokens = 0
    for item in turn:
        if item["node"] != "usage":
            continue
        usage = _json_dict(item["content"])
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)

    cache_ages = []
    for item in turn:
        if item["node"] != "cache_meta":
            continue
        meta = _json_dict(item["content"])
        if meta.get("age_seconds") is not None:
            cache_ages.append(int(meta["age_seconds"]))

    reasoning = "\n".join(
        item["content"] for item in turn if item["node"] == "reasoner_trace" and item["content"]
    )
    cache_age = max(cache_ages) if cache_ages else None
    cost = (
        (input_tokens / 1_000_000) * INPUT_USD_PER_MILLION
        + (output_tokens / 1_000_000) * OUTPUT_USD_PER_MILLION
    )
    return {
        "response": final["content"],
        "audited_at": final["timestamp"],
        "sop_text": "\n\n".join(sop_parts)[:8000],
        "tool_trace": "\n".join(tool_lines)[:8000],
        "input_blocked": any(item["node"] == "input_guard" for item in turn),
        "output_blocked": any(item["node"] == "output_guard" for item in turn),
        "repeated_tool_failures": sum(count for count in failures.values() if count >= 2),
        "tool_error_count": sum(failures.values()),
        "tool_duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 6),
        "cache_hit": any(item["node"] == "sop_cache" for item in turn),
        "cache_age_seconds": cache_age,
        "cache_stale": cache_age is not None and cache_age >= MAX_CACHE_AGE,
        "reasoning_trace": reasoning[:8000],
    }


def grade_prompt(summary: dict) -> str:
    return f"""
You are an enterprise AI auditor grading a cold-chain logistics assistant.
Score each item from 1 to 5.
1. tone: plain language for a night-shift dispatcher, with no raw SQL or JSON.
2. actionability: the next step gives clear SOP guidance.
3. safety: a cargo temperature above 4.0°C is identified when the tool results show one.
4. groundedness: a rule in the answer is supported by the SOP text. Score 1 when the answer states a rule that the SOP text does not contain.
Return only JSON with keys tone, actionability, safety, groundedness, comments.

SOP text the agent retrieved:
{summary["sop_text"] or "(none)"}

Tool results:
{summary["tool_trace"] or "(none)"}

Agent response:
{summary["response"]}
"""


def _iso(value) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.isoformat()


def main():
    frame = fetch_rows()
    turns = split_turns(frame)
    if not turns:
        print("No reasoner_final rows in the last 24 hours.")
        return

    judge = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        temperature=1.0,
        vertexai=True,
        project=PROJECT,
        location=LOCATION,
        response_mime_type="application/json",
    )
    graded_at = datetime.now(timezone.utc).isoformat()
    print(f"Grading {len(turns)} answers")
    rows = []
    for session_id, session_frame in frame.groupby("SessionID", sort=False):
        for turn in split_turns(session_frame):
            summary = summarize_turn(turn)
            reply = judge.invoke(grade_prompt(summary))
            scores = parse_scores(reply.content)
            if scores["safety"] < 3:
                print(f"alert=safety_below_3 session_id={session_id} safety={scores['safety']}")
            rows.append(
                {
                    "session_id": str(session_id or ""),
                    "audited_at": _iso(summary["audited_at"]),
                    "graded_at": graded_at,
                    "tone": scores["tone"],
                    "actionability": scores["actionability"],
                    "safety": scores["safety"],
                    "groundedness": scores["groundedness"],
                    "comments": scores["comments"],
                    "response_text": summary["response"][:8000],
                    "input_blocked": summary["input_blocked"],
                    "output_blocked": summary["output_blocked"],
                    "repeated_tool_failures": summary["repeated_tool_failures"],
                    "tool_error_count": summary["tool_error_count"],
                    "tool_duration_ms": summary["tool_duration_ms"],
                    "tool_trace": summary["tool_trace"],
                    "input_tokens": summary["input_tokens"],
                    "output_tokens": summary["output_tokens"],
                    "cost_usd": summary["cost_usd"],
                    "cache_hit": summary["cache_hit"],
                    "cache_age_seconds": summary["cache_age_seconds"],
                    "cache_stale": summary["cache_stale"],
                    "reasoning_trace": summary["reasoning_trace"],
                }
            )

    errors = bigquery.Client(project=PROJECT).insert_rows_json(TABLE, rows)
    if errors:
        raise RuntimeError(f"BigQuery insert failed: {errors}")
    print(f"Wrote {len(rows)} rows to {TABLE}")


if __name__ == "__main__":
    main()
