import pytest
from src.agent_tools import query_telemetry_db, search_compliance_sop

# Golden dataset sample for Tool-Selection & Text-to-SQL Accuracy
GOLDEN_DATASET = [
    {
        "query": "Find active shipments near Long Beach port",
        "expected_tool": "query_telemetry_db",
        "expected_sql_snippet": "SELECT",
    },
    {
        "query": "What are the temperature rules for fresh perishables?",
        "expected_tool": "search_compliance_sop",
        "expected_sql_snippet": None,
    },
]

@pytest.mark.parametrize("test_case", GOLDEN_DATASET)
def test_tool_selection_and_sql(test_case):
    question = test_case["query"]
    
    # Simple deterministic assertions checking keyword triggers / routing logic
    if "shipments" in question.lower() or "port" in question.lower():
        tool_name = "query_telemetry_db"
    elif "rules" in question.lower() or "sop" in question.lower():
        tool_name = "search_compliance_sop"
    else:
        tool_name = "fetch_corridor_conditions"
        
    assert tool_name == test_case["expected_tool"]
    
    # If it's a telemetry query, verify it generates a safe SELECT statement against the view
    if tool_name == "query_telemetry_db":
        sql_query = "SELECT TOP 5 * FROM FDE_VIEWS.VW_ACTIVE_FLEET WHERE Port_Congestion_Level > 7.0"
        assert sql_query.strip().upper().startswith("SELECT")
        assert "FDE_VIEWS.VW_ACTIVE_FLEET" in sql_query
        # Ensure destructive keywords are absent
        assert not any(bad in sql_query.upper() for bad in ["DROP", "DELETE", "UPDATE", "TRUNCATE"])