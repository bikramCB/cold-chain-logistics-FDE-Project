import requests

from src.agent_tools import (
    fetch_corridor_conditions,
    is_tool_failure,
    query_telemetry_db,
    search_compliance_sop,
    sql_breaker,
    tool_turn,
    weather_breaker,
)
from src.guardrails import check_answer


def test_sql_rejects_writes_and_batches():
    assert "SECURITY BLOCK" in query_telemetry_db.invoke("DROP TABLE FDE_VIEWS.VW_ACTIVE_FLEET")
    assert "one SELECT" in query_telemetry_db.invoke(
        "SELECT TOP 1 * FROM FDE_VIEWS.VW_ACTIVE_FLEET; DROP TABLE FDE_VIEWS.VW_ACTIVE_FLEET"
    )


def test_weather_rejects_impossible_coordinates():
    result = fetch_corridor_conditions.invoke({"latitude": 140, "longitude": 0})
    assert result.startswith("error: Invalid coordinates")


def test_each_tool_stops_after_two_calls_in_one_turn():
    with tool_turn():
        fetch_corridor_conditions.invoke({"latitude": 140, "longitude": 0})
        fetch_corridor_conditions.invoke({"latitude": 140, "longitude": 0})
        third = fetch_corridor_conditions.invoke({"latitude": 140, "longitude": 0})
    assert "already ran 2 times" in third


def test_sop_search_rejects_an_empty_question():
    assert "needs a question" in search_compliance_sop.invoke("   ")


def test_weather_retries_one_timeout_then_uses_the_reply(monkeypatch):
    calls = {"n": 0}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"current_weather": {"temperature": 21, "windspeed": 3}}

    def fake_get(url, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.Timeout("timed out")
        return Response()

    monkeypatch.setattr("src.agent_tools.requests.get", fake_get)
    monkeypatch.setattr("src.agent_tools.time.sleep", lambda _seconds: None)
    weather_breaker.reset()
    result = fetch_corridor_conditions.invoke({"latitude": 33.8, "longitude": -118.1})
    assert calls["n"] == 2
    assert "External Temp: 21" in result


def test_breaker_opens_after_repeated_connection_failures():
    sql_breaker.reset()
    sql_breaker.record_failure()
    sql_breaker.record_failure()
    assert sql_breaker.allow() is None
    sql_breaker.record_failure()
    assert "unavailable" in sql_breaker.allow()
    sql_breaker.reset()


def test_failure_text_is_marked_for_the_audit_log():
    assert is_tool_failure("error: Database Error: timeout")
    assert is_tool_failure("error: Corridor API Communication Failure: timed out")
    assert not is_tool_failure("COLUMNS: Latitude\n(33.8,)")


def test_unreadable_shipment_records_block_a_cargo_claim():
    traces = [{
        "type": "tool_output",
        "name": "query_telemetry_db",
        "content": "error: Database Error: Login timeout expired",
    }]
    issues = check_answer("The cargo is safe at 3°C.", traces)
    assert any("Shipment records were not retrieved" in item for item in issues)
