"""Create FDE_VIEWS.AgentAuditLog if Phase 4 SQL was not run by hand.

Connects as SQL_ADMIN_USER from .env and grants INSERT to USR_FDE_RO.
Safe to run more than once.
"""
import os
import urllib
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

script_dir = Path(__file__).resolve().parent
project_root = script_dir.parent

load_dotenv(project_root / ".env")

db_host = os.getenv("SQL_SERVER_HOST", "localhost")
db_port = os.getenv("SQL_SERVER_PORT", "1433")
db_user = os.getenv("SQL_ADMIN_USER")
db_password = os.getenv("SQL_ADMIN_PASSWORD")
agent_user = os.getenv("SQL_AGENT_USER", "USR_FDE_RO")

if not db_user or not db_password:
    raise SystemExit("SQL_ADMIN_USER and SQL_ADMIN_PASSWORD must be set in .env")

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

print(f"Connecting to {db_host},{db_port} as {db_user}...")

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    schema_exists = conn.execute(
        text("SELECT CASE WHEN SCHEMA_ID('FDE_VIEWS') IS NULL THEN 0 ELSE 1 END")
    ).scalar()
    if not schema_exists:
        raise SystemExit(
            "Schema FDE_VIEWS does not exist. Run scripts/setup_security_and_view.sql first."
        )

    existed = conn.execute(
        text("SELECT CASE WHEN OBJECT_ID('FDE_VIEWS.AgentAuditLog', 'U') IS NULL THEN 0 ELSE 1 END")
    ).scalar()

    if not existed:
        conn.execute(text("""
            CREATE TABLE FDE_VIEWS.AgentAuditLog (
                LogID INT IDENTITY(1,1) PRIMARY KEY,
                Timestamp DATETIME DEFAULT GETDATE(),
                SessionID VARCHAR(50),
                NodeExecuted VARCHAR(50),
                ToolName VARCHAR(100),
                Content NVARCHAR(MAX)
            )
        """))
        print("Created FDE_VIEWS.AgentAuditLog")
    else:
        print("FDE_VIEWS.AgentAuditLog already exists")

    conn.execute(text(f"GRANT INSERT ON FDE_VIEWS.AgentAuditLog TO {agent_user}"))
    row_count = conn.execute(text("SELECT COUNT(*) FROM FDE_VIEWS.AgentAuditLog")).scalar()

print(f"INSERT granted to {agent_user}. Current row count: {row_count}")
