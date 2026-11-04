import os
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from sqlalchemy import create_engine, text

project_root = Path(__file__).resolve().parents[1]
load_dotenv(project_root / ".env")


class NoAuditLogs(Exception):
    """Raised when the audit table has no final agent responses to grade."""


def run_nightly_audit_evaluation():
    # 1. Connect to SQL Server using admin credentials to read AgentAuditLog
    db_host = os.getenv("SQL_SERVER_HOST", "localhost")
    db_port = os.getenv("SQL_SERVER_PORT", "1433")
    db_user = os.getenv("SQL_ADMIN_USER")
    db_password = os.getenv("SQL_ADMIN_PASSWORD")
    if not db_user or not db_password:
        raise RuntimeError("SQL_ADMIN_USER and SQL_ADMIN_PASSWORD must be set in .env")

    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION")
    if not project or not location:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION must be set in .env"
        )

    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={db_host},{db_port};"
        "DATABASE=master;"
        f"UID={db_user};"
        f"PWD={db_password};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )
    params = urllib.parse.quote_plus(connection_string)
    engine = create_engine(f"mssql+pyodbc:///?odbc_connect={params}")

    # Fetch recent agent final responses from the audit table
    query = text(
        """
        SELECT TOP 10 SessionID, Timestamp, Content
        FROM FDE_VIEWS.AgentAuditLog
        WHERE NodeExecuted = 'reasoner_final'
        ORDER BY Timestamp DESC
        """
    )

    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn)
    except Exception as exc:
        raise RuntimeError(
            f"Could not read FDE_VIEWS.AgentAuditLog on {db_host}:{db_port}. "
            f"{type(exc).__name__}: the SQL host did not accept the connection."
        ) from None

    if df.empty:
        raise NoAuditLogs("No audit logs found to evaluate.")

    # 2. Same Vertex Gemini model the agent uses
    judge_llm = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        temperature=1.0,
        vertexai=True,
        project=project,
        location=location,
    )

    print(f"Running LLM-as-a-Judge evaluation on {len(df)} recent agent interactions...")

    for _, row in df.iterrows():
        agent_response = row["Content"]
        session_id = str(row["SessionID"] or "")
        session_label = session_id[:8] or "unknown"

        grading_prompt = f"""
        You are an enterprise AI auditor grading a cold-chain logistics assistant's response.
        Evaluate the response below across three criteria, returning a score from 1 to 5 and a short reason for each:
        1. Tone & Simplicity: Is it free of raw technical jargon, SQL, or JSON, tailored for a night-shift dispatcher?
        2. Actionability: Does the 'What to do next' section provide clear SOP guidance?
        3. Safety Compliance: Did it correctly identify temperature breaches (>4.0°C) if present?

        Agent Response:
        {agent_response}

        Format output as:
        - Tone Score: [1-5]
        - Actionability Score: [1-5]
        - Safety Score: [1-5]
        - Comments: [Notes]
        """

        evaluation = judge_llm.invoke(grading_prompt)
        print(f"\n--- Evaluation for Session {session_label} ---")
        print(evaluation.content)


def test_nightly_audit_evaluation():
    try:
        run_nightly_audit_evaluation()
    except NoAuditLogs as exc:
        pytest.skip(str(exc))


if __name__ == "__main__":
    run_nightly_audit_evaluation()
