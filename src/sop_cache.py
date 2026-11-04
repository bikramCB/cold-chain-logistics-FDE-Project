"""Short-lived cache for SOP-only answers.

Each dispatcher session has its own entries. The cache is never shared across
sessions, and it never stores telemetry or weather answers.

Two clocks, same idea as a Redis key with a TTL:

- Idle: if this session asks nothing for SOP_CACHE_IDLE_SECONDS, every saved
  answer for that session is deleted.
- Max age: a saved answer is deleted SOP_CACHE_MAX_AGE_SECONDS after it was
  stored, even if the same person is still asking questions.

Expired rows are removed on the next lookup. Nothing here is written to last.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass

LIVE_TOOLS = frozenset({"query_telemetry_db", "fetch_corridor_conditions"})
SOP_TOOL = "search_compliance_sop"


def is_sop_only(traces: list[dict]) -> bool:
    """True only when the turn used the rule book and no live fleet or weather tool."""
    names = {trace.get("name") for trace in traces if trace.get("name")}
    if SOP_TOOL not in names:
        return False
    return names.isdisjoint(LIVE_TOOLS)


def normalize_question(question: str) -> str:
    folded = question.lower().strip()
    folded = re.sub(r"[^\w\s]", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


@dataclass
class _Entry:
    answer: str
    created_at: float


@dataclass
class CacheHit:
    answer: str


class SopAnswerCache:
    def __init__(self, idle_seconds: int, max_age_seconds: int, now=None):
        if idle_seconds <= 0 or max_age_seconds <= 0:
            raise ValueError("Cache lifetimes must be greater than zero.")
        self.idle_seconds = idle_seconds
        self.max_age_seconds = max_age_seconds
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._last_active: dict[str, float] = {}

    def begin_turn(self, session_id: str) -> None:
        """Drop this session's answers if the person has been idle, then mark them active."""
        with self._lock:
            self._expire_locked(session_id, self._now())
            self._last_active[session_id] = self._now()

    def get(self, session_id: str, question: str) -> CacheHit | None:
        key = (session_id, _question_key(question))
        with self._lock:
            now = self._now()
            self._expire_locked(session_id, now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            return CacheHit(answer=entry.answer)

    def put(self, session_id: str, question: str, answer: str) -> None:
        text = (answer or "").strip()
        if not text or not normalize_question(question):
            return
        key = (session_id, _question_key(question))
        with self._lock:
            now = self._now()
            self._expire_locked(session_id, now)
            self._entries[key] = _Entry(answer=text, created_at=now)
            self._last_active[session_id] = now

    def drop_session(self, session_id: str) -> None:
        with self._lock:
            self._drop_session_locked(session_id)

    def _expire_locked(self, session_id: str, now: float) -> None:
        last_active = self._last_active.get(session_id)
        if last_active is not None and now - last_active >= self.idle_seconds:
            self._drop_session_locked(session_id)
            return
        stale = [
            key
            for key, entry in self._entries.items()
            if key[0] == session_id and now - entry.created_at >= self.max_age_seconds
        ]
        for key in stale:
            del self._entries[key]
    def _drop_session_locked(self, session_id: str) -> None:
        for key in [key for key in self._entries if key[0] == session_id]:
            del self._entries[key]
        self._last_active.pop(session_id, None)


def _question_key(question: str) -> str:
    normalized = normalize_question(question)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _env_seconds(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


sop_cache = SopAnswerCache(
    idle_seconds=_env_seconds("SOP_CACHE_IDLE_SECONDS", 7200),
    max_age_seconds=_env_seconds("SOP_CACHE_MAX_AGE_SECONDS", 28800),
)
