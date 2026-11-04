"""SOP answer cache: per session, idle expiry, and a hard maximum age."""
from src.sop_cache import SopAnswerCache, is_sop_only


def _clock(start=0.0):
    state = {"now": start}

    def now():
        return state["now"]

    def advance(seconds):
        state["now"] += seconds

    return now, advance


def test_same_session_reuses_sop_answer():
    now, _ = _clock()
    cache = SopAnswerCache(idle_seconds=100, max_age_seconds=500, now=now)
    cache.begin_turn("session-a")
    cache.put("session-a", "What is a Tier 1 escalation?", "Tier 1 is the first call.")
    cache.begin_turn("session-a")
    hit = cache.get("session-a", "what is a tier 1 escalation")
    assert hit is not None
    assert hit.answer == "Tier 1 is the first call."


def test_another_session_does_not_see_it():
    now, _ = _clock()
    cache = SopAnswerCache(idle_seconds=100, max_age_seconds=500, now=now)
    cache.begin_turn("session-a")
    cache.put("session-a", "What is a Tier 1 escalation?", "Tier 1 is the first call.")
    cache.begin_turn("session-b")
    assert cache.get("session-b", "What is a Tier 1 escalation?") is None


def test_idle_session_loses_its_answers():
    now, advance = _clock()
    cache = SopAnswerCache(idle_seconds=100, max_age_seconds=500, now=now)
    cache.begin_turn("session-a")
    cache.put("session-a", "Explain Tier 2", "Tier 2 goes to the duty manager.")
    advance(100)
    cache.begin_turn("session-a")
    assert cache.get("session-a", "Explain Tier 2") is None


def test_active_user_still_hits_until_max_age():
    now, advance = _clock()
    cache = SopAnswerCache(idle_seconds=100, max_age_seconds=150, now=now)
    cache.begin_turn("session-a")
    cache.put("session-a", "Explain Tier 2", "Tier 2 goes to the duty manager.")
    advance(90)
    cache.begin_turn("session-a")
    assert cache.get("session-a", "Explain Tier 2") is not None
    advance(70)
    cache.begin_turn("session-a")
    assert cache.get("session-a", "Explain Tier 2") is None


def test_drop_session_removes_answers_immediately():
    now, _ = _clock()
    cache = SopAnswerCache(idle_seconds=100, max_age_seconds=500, now=now)
    cache.begin_turn("session-a")
    cache.put("session-a", "Explain Tier 2", "Tier 2 goes to the duty manager.")
    cache.drop_session("session-a")
    cache.begin_turn("session-a")
    assert cache.get("session-a", "Explain Tier 2") is None


def test_only_sop_traces_qualify():
    sop = [{"name": "search_compliance_sop", "type": "tool_output"}]
    mixed = [
        {"name": "search_compliance_sop", "type": "tool_output"},
        {"name": "query_telemetry_db", "type": "tool_output"},
    ]
    weather = [{"name": "fetch_corridor_conditions", "type": "tool_output"}]
    assert is_sop_only(sop)
    assert not is_sop_only(mixed)
    assert not is_sop_only(weather)
    assert not is_sop_only([])
