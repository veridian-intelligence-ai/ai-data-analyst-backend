"""End-to-end tests for POST /chat with the orchestrator and store faked.

test_main_auth.py already pins the walls around /chat (401/403/429). This
file drives the happy paths and pins two contracts nothing else covers:

1. The response/persistence wiring: the orchestrator's result flows into the
   response envelope, the assistant message is persisted through
   append_assistant_message, and emit_usage_event receives the turn's real
   token totals (the metering source of truth).
2. The log grammar. print-to-stdout is this service's ONLY observability
   channel, and the failure drills (Mission M29) diagnose incidents by
   grepping these exact prefixes — [timing], [llm], [visual]. A renamed or
   dropped line passes every other test and only fails at 2am.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.auth.sessions as sessions
import app.main as main
from app.ai.orchestrator import IterationLog, OrchestratorResult

client = TestClient(main.app, raise_server_exceptions=False)

_AUTH = {"Authorization": "Bearer fake-session-token"}

_ACTIVE_USER: dict[str, Any] = {
    "id": 7,
    "email": "analyst@acme.example",
    "name": "Ana Lyst",
    "role": "member",
    "status": "active",
}


class RecordingStore:
    """Store fake that records the calls /chat makes, in order."""

    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []
        self.assistant_calls: list[dict[str, Any]] = []
        self.usage_events: list[dict[str, Any]] = []

    def check_rate_limit(self, *a: Any, **k: Any) -> bool:
        return True

    def ensure_session(self, *a: Any, **k: Any) -> None:
        return None

    def check_session_owner(self, *a: Any, **k: Any) -> bool:
        return True

    def get_history(self, *a: Any, **k: Any) -> list[dict[str, str]]:
        return []

    def append_message(self, session_id: str, role: str, content: str) -> None:
        self.appended.append((session_id, role, content))

    def append_assistant_message(self, session_id: str, **kwargs: Any) -> None:
        self.assistant_calls.append({"session_id": session_id, **kwargs})

    def emit_usage_event(self, **kwargs: Any) -> None:
        self.usage_events.append(kwargs)


class FakeOrchestrator:
    """Stands in for DAXConversationOrchestrator: returns a preset result."""

    result: OrchestratorResult = OrchestratorResult(answer="not configured")

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def run(self, message: str, conversation_history: list[Any]) -> OrchestratorResult:
        return type(self).result


@pytest.fixture
def chat_harness(monkeypatch: pytest.MonkeyPatch) -> RecordingStore:
    """Authenticated user + recording store + faked orchestrator/adapters."""
    monkeypatch.setattr(sessions, "validate_token", lambda token: dict(_ACTIVE_USER))
    store = RecordingStore()
    monkeypatch.setattr(main, "_store", store)
    monkeypatch.setattr(main, "DAXConversationOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(main, "_get_powerbi_client", lambda: object())
    monkeypatch.setattr(main, "_get_anthropic_client", lambda: object())
    return store


def _post_chat(message: str = "How did sales do?") -> Any:
    return client.post("/chat", json={"session_id": "s1", "message": message}, headers=_AUTH)


def _iteration(n: int, **overrides: Any) -> IterationLog:
    defaults: dict[str, Any] = {
        "iteration": n,
        "latency_ms": 100,
        "stop_reason": "end_turn",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }
    defaults.update(overrides)
    return IterationLog(**defaults)


# ── Response and persistence wiring ─────────────────────────────────────


def test_text_answer_flows_into_envelope_and_persistence(
    chat_harness: RecordingStore,
) -> None:
    FakeOrchestrator.result = OrchestratorResult(
        answer="Revenue was 1.2M.",
        next_step_suggestions=["Break it down by region"],
        total_input_tokens=200,
        total_output_tokens=90,
        total_cache_read_tokens=15_000,
        total_cache_write_tokens=0,
    )
    res = _post_chat()
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["answer"] == "Revenue was 1.2M."
    assert body["response_mode"] == "text"
    assert body["visual"] is None
    assert body["next_step_suggestions"] == ["Break it down by region"]
    assert body["error"] is None

    # The user message was persisted before the turn, the assistant after.
    assert chat_harness.appended == [("s1", "user", "How did sales do?")]
    assert len(chat_harness.assistant_calls) == 1
    persisted = chat_harness.assistant_calls[0]
    assert persisted["session_id"] == "s1"
    assert persisted["response_mode"] == "text"
    assert persisted["visual"] is None


def test_visual_answer_builds_payload_and_logs_built(
    chat_harness: RecordingStore, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeOrchestrator.result = OrchestratorResult(
        answer="Top brands by revenue below.",
        visual_hint={
            "chart_type": "bar",
            "x_field": "brand",
            "y_field": "revenue",
            "title": "Revenue by brand",
            "value_format": "currency",
        },
        dax_rows=[{"brand": "North", "revenue": 10.0}, {"brand": "South", "revenue": 7.5}],
        dax_columns=["brand", "revenue"],
    )
    res = _post_chat()
    assert res.status_code == 200
    body = res.json()
    assert body["response_mode"] == "visual"
    assert body["visual"]["visual_type"] == "chart"
    assert body["visual"]["chart_type"] == "bar"

    out = capsys.readouterr().out
    assert "[visual] built type=bar" in out


def test_usage_event_carries_the_turns_token_totals(
    chat_harness: RecordingStore,
) -> None:
    FakeOrchestrator.result = OrchestratorResult(
        answer="Done.",
        total_input_tokens=321,
        total_output_tokens=123,
        total_cache_read_tokens=34_500,
        total_cache_write_tokens=17,
    )
    assert _post_chat().status_code == 200
    assert len(chat_harness.usage_events) == 1
    event = chat_harness.usage_events[0]
    assert event["endpoint"] == "/chat"
    assert event["user_id"] == _ACTIVE_USER["id"]
    assert event["input_tokens"] == 321
    assert event["output_tokens"] == 123
    assert event["cache_read_tokens"] == 34_500
    assert event["cache_write_tokens"] == 17


# ── Log grammar (the M29 drills grep these exact prefixes) ──────────────


def test_timing_log_line_grammar(
    chat_harness: RecordingStore, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeOrchestrator.result = OrchestratorResult(
        answer="ok",
        total_latency_ms=1234,
        iterations=[
            _iteration(1, latency_ms=1000, stop_reason="tool_use", dax_latency_ms=200),
            _iteration(2, latency_ms=234, stop_reason="end_turn"),
        ],
    )
    assert _post_chat().status_code == 200
    out = capsys.readouterr().out
    timing_lines = [line for line in out.splitlines() if line.startswith("[timing] ")]
    assert len(timing_lines) == 1, f"expected one [timing] line, got: {out!r}"
    line = timing_lines[0]
    # Counts make a retry storm visible: 2 iterations, but only 1 executed query.
    for fragment in (
        "total=",
        "llm=1234ms (2 iterations)",
        "powerbi=200ms (1 queries)",
        "other=",
    ):
        assert fragment in line


def test_visual_skip_log_when_no_hint(
    chat_harness: RecordingStore, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeOrchestrator.result = OrchestratorResult(
        answer="Plain prose answer.",
        dax_rows=[{"v": 1}],
        dax_columns=["v"],
    )
    assert _post_chat().status_code == 200
    out = capsys.readouterr().out
    # The skip line must carry the real row count and column names — that
    # detail is what makes a phase-2 "why no chart?" diagnosable from logs.
    assert "[visual] skipped reason=no_visual_hint_from_model rows=1 cols=['v']" in out
