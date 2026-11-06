"""Grade the last 24 hours of dispatcher answers and store the scores."""
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


def parse_scores(raw):
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Judge did not return JSON")
    data = json.loads(cleaned[start : end + 1])
    return {
        "tone": int(data["tone"]),
        "actionability": int(data["actionability"]),
        "safety": int(data["safety"]),
        "comments": str(data.get("comments", ""))[:2000],
    }


def fetch_answers():
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
        SELECT SessionID, Timestamp, Content
        FROM FDE_VIEWS.AgentAuditLog
        WHERE NodeExecuted = 'reasoner_final'
          AND Timestamp >= DATEADD(hour, -24, GETDATE())
        ORDER BY Timestamp DESC
        """
    )
    with engine.connect() as conn:
        return pd.read_sql(query, conn)


def main():
    answers = fetch_answers()
    if answers.empty:
        print("No reasoner_final rows in the last 24 hours.")
        return

    judge = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        temperature=1.0,
        vertexai=True,
        project=PROJECT,
        location=LOCATION,
    )
    rows = []
    graded_at = datetime.now(timezone.utc).isoformat()
    print(f"Grading {len(answers)} answers")

    for _, row in answers.iterrows():
        session_id = str(row["SessionID"] or "")
        response = str(row["Content"] or "")
        prompt = f"""
You are an enterprise AI auditor grading a cold-chain logistics assistant.
Score the response from 1 to 5 on each criterion:
1. tone: plain language for a night-shift dispatcher, with no raw SQL or JSON.
2. actionability: the next step gives clear SOP guidance.
3. safety: temperature breaches above 4.0°C are identified when present.
Return only JSON with keys tone, actionability, safety, comments.

Agent response:
{response}
"""
        scores = parse_scores(judge.invoke(prompt).content)
        audited_at = pd.Timestamp(row["Timestamp"]).tz_localize("UTC").isoformat()
        rows.append(
            {
                "session_id": session_id,
                "audited_at": audited_at,
                "graded_at": graded_at,
                "tone": scores["tone"],
                "actionability": scores["actionability"],
                "safety": scores["safety"],
                "comments": scores["comments"],
                "response_text": response[:8000],
            }
        )
        if scores["safety"] < 3:
            print(f"alert=safety_below_3 session_id={session_id} safety={scores['safety']}")

    errors = bigquery.Client(project=PROJECT).insert_rows_json(TABLE, rows)
    if errors:
        raise RuntimeError(f"BigQuery insert failed: {errors}")
    print(f"Wrote {len(rows)} rows to {TABLE}")


if __name__ == "__main__":
    main()
