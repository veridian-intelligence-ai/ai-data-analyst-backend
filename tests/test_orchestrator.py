"""
Unit tests for DAXConversationOrchestrator (app/ai/orchestrator.py).

Both externals are faked: a scripted Anthropic client (each call pops the
next scripted response or raises the scripted exception) and a recording
Power BI client. The suite pins the loop's contract: errors are FOOD (guard
rejections and Power BI errors go back as is_error tool results), rows are
capped in tool results, exhaustion and API errors degrade honestly, and the
guard short-circuits BEFORE any Power BI round trip.
"""
from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import pytest

from app.adapters.base import QueryResult
from app.adapters.powerbi.exceptions import PowerBIAPIError
from app.ai.orchestrator import (
    _MAX_ITERATIONS,
    _MAX_ROWS_IN_TOOL_RESULT,
    DAXConversationOrchestrator,
    IterationLog,
    OrchestratorResult,
    _extract_suggestions,
)

# ── Fakes ───────────────────────────────────────────────────────────────


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(
        self,
        query: str,
        explanation: str = "test explanation",
        visual_hint: dict[str, Any] | None = None,
        block_id: str = "toolu_1",
        name: str = "execute_dax",
    ) -> None:
        self.id = block_id
        self.name = name
        self.input: dict[str, Any] = {"query": query, "explanation": explanation}
        if visual_hint is not None:
            self.input["visual_hint"] = visual_hint


class _Usage:
    def __init__(self, **tokens: int) -> None:
        self.input_tokens = tokens.get("input_tokens", 100)
        self.output_tokens = tokens.get("output_tokens", 50)
        self.cache_creation_input_tokens = tokens.get("cache_creation_input_tokens", 0)
        self.cache_read_input_tokens = tokens.get("cache_read_input_tokens", 0)


class _Response:
    def __init__(self, stop_reason: str, content: list[Any], **tokens: int) -> None:
        self.stop_reason = stop_reason
        self.content = content
        self.usage = _Usage(**tokens)


def _end_turn(text: str, **tokens: int) -> _Response:
    return _Response("end_turn", [_TextBlock(text)], **tokens)


def _tool_use(block: _ToolUseBlock, pre_text: str | None = None, **tokens: int) -> _Response:
    content: list[Any] = []
    if pre_text:
        content.append(_TextBlock(pre_text))
    content.append(block)
    return _Response("tool_use", content, **tokens)


class FakeAnthropicClient:
    """Scripted client: each messages.create pops the next outcome."""

    class _Messages:
        def __init__(self, outcomes: list[_Response | Exception]) -> None:
            self._outcomes = list(outcomes)
            self.calls: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> _Response:
            # The orchestrator reuses (and mutates) one messages list across
            # iterations — snapshot it, or every recorded call would show the
            # final state.
            recorded = dict(kwargs)
            recorded["messages"] = list(kwargs.get("messages", []))
            self.calls.append(recorded)
            if not self._outcomes:
                raise AssertionError("orchestrator called the LLM more times than scripted")
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    def __init__(self, outcomes: list[_Response | Exception]) -> None:
        self.messages = self._Messages(outcomes)


class FakePowerBIClient:
    """Recording adapter: each execute_query pops the next outcome."""

    def __init__(self, outcomes: list[QueryResult | Exception] | None = None) -> None:
        self._outcomes = list(outcomes or [])
        self.executed: list[dict[str, str]] = []

    def execute_query(self, model_id: str, query: str) -> QueryResult:
        self.executed.append({"model_id": model_id, "query": query})
        if not self._outcomes:
            raise AssertionError("adapter called more times than scripted")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _query_result(rows: list[dict[str, Any]], latency_ms: int = 850) -> QueryResult:
    columns = list(rows[0].keys()) if rows else []
    return QueryResult(
        columns=columns, rows=rows, row_count=len(rows), metadata={"latency_ms": latency_ms}
    )


def _make_orchestrator(
    llm: FakeAnthropicClient, powerbi: FakePowerBIClient
) -> DAXConversationOrchestrator:
    return DAXConversationOrchestrator(
        anthropic_client=llm,  # type: ignore[arg-type]
        powerbi_client=powerbi,  # type: ignore[arg-type]
        dataset_id="fake-dataset-id-33333333",
    )


def _last_tool_result(llm: FakeAnthropicClient, call_index: int = -1) -> dict[str, Any]:
    """The tool_result content dict sent back to the LLM on a given call."""
    messages = llm.messages.calls[call_index]["messages"]
    tool_results = messages[-1]["content"]
    return json.loads(tool_results[0]["content"])


# ── Construction / input guards ─────────────────────────────────────────


def test_requires_dataset_id() -> None:
    with pytest.raises(ValueError):
        DAXConversationOrchestrator(
            anthropic_client=FakeAnthropicClient([]),  # type: ignore[arg-type]
            powerbi_client=FakePowerBIClient(),  # type: ignore[arg-type]
            dataset_id="  ",
        )


def test_empty_question_short_circuits_without_llm_call() -> None:
    llm = FakeAnthropicClient([])
    orchestrator = _make_orchestrator(llm, FakePowerBIClient())
    result = orchestrator.run("   ")
    assert result.error == "empty_user_message"
    assert llm.messages.calls == []


# ── Happy path ──────────────────────────────────────────────────────────


def test_happy_path_tool_use_then_end_turn() -> None:
    hint = {"chart_type": "bar", "x_field": "category", "y_field": "Revenue"}
    rows = [{"category": "Hardware", "Revenue": 1200.0}, {"category": "Software", "Revenue": 800.0}]
    llm = FakeAnthropicClient(
        [
            _tool_use(
                _ToolUseBlock("EVALUATE SUMMARIZECOLUMNS(...)", visual_hint=hint),
                pre_text="Let me query that.",
                cache_creation_input_tokens=34000,
            ),
            _end_turn(
                "Hardware leads with 1,200.\n\n[next_step_suggestions]\n"
                "- Break it down by month\n- Compare with last year\n- Show the top products",
                cache_read_input_tokens=34000,
            ),
        ]
    )
    powerbi = FakePowerBIClient([_query_result(rows)])
    result = _make_orchestrator(llm, powerbi).run("Revenue by category?")

    assert result.error is None
    assert result.answer == "Hardware leads with 1,200."
    assert result.next_step_suggestions == [
        "Break it down by month",
        "Compare with last year",
        "Show the top products",
    ]
    assert result.dax_query == "EVALUATE SUMMARIZECOLUMNS(...)"
    assert result.dax_rows == rows
    assert result.dax_columns == ["category", "Revenue"]
    assert result.visual_hint == hint
    assert powerbi.executed == [
        {"model_id": "fake-dataset-id-33333333", "query": "EVALUATE SUMMARIZECOLUMNS(...)"}
    ]
    # Token/cache accounting aggregates across iterations. All four totals
    # feed emit_usage_event — the metering rows are only as honest as these
    # sums (fake usage defaults: in=100/out=50 per iteration, two iterations).
    assert len(result.iterations) == 2
    assert result.iterations[0].pre_tool_text == "Let me query that."
    assert result.total_cache_write_tokens == 34000
    assert result.total_cache_read_tokens == 34000
    assert result.total_input_tokens == 200
    assert result.total_output_tokens == 100
    # The successful tool result was NOT flagged as an error.
    tool_result = _last_tool_result(llm)
    assert tool_result["ok"] is True
    assert tool_result["row_count"] == 2


# ── Errors are food ─────────────────────────────────────────────────────


def test_guard_rejection_short_circuits_without_adapter_call() -> None:
    llm = FakeAnthropicClient(
        [
            _tool_use(_ToolUseBlock("EVALUATE {[Revenue]}")),
            _tool_use(_ToolUseBlock('EVALUATE ROW("Revenue", [Revenue])')),
            _end_turn("Revenue is 500."),
        ]
    )
    powerbi = FakePowerBIClient([_query_result([{"Revenue": 500.0}])])
    result = _make_orchestrator(llm, powerbi).run("Total revenue?")

    # The forbidden query never reached Power BI — only the corrected one did.
    assert [call["query"] for call in powerbi.executed] == ['EVALUATE ROW("Revenue", [Revenue])']

    # The rejection travelled back to the model as an is_error tool result
    # carrying the guard's instructive message.
    messages = llm.messages.calls[1]["messages"]
    rejection = messages[-1]["content"][0]
    assert rejection["is_error"] is True
    body = json.loads(rejection["content"])
    assert body["error_type"] == "DaxValidationError"
    assert "anonymous columns" in body["error_message"]

    assert result.error is None
    assert result.dax_query == 'EVALUATE ROW("Revenue", [Revenue])'


def test_powerbi_error_feeds_back_and_model_self_corrects() -> None:
    llm = FakeAnthropicClient(
        [
            _tool_use(_ToolUseBlock("EVALUATE ROW(\"x\", [Bad Measure])")),
            _tool_use(_ToolUseBlock("EVALUATE ROW(\"x\", [Good Measure])")),
            _end_turn("The value is 7."),
        ]
    )
    powerbi = FakePowerBIClient(
        [
            PowerBIAPIError(
                "DAX query invalid",
                context={"status_code": 400, "body": "Column [Bad Measure] cannot be found"},
            ),
            _query_result([{"x": 7}]),
        ]
    )
    result = _make_orchestrator(llm, powerbi).run("What is x?")

    error_result = _last_tool_result(llm, call_index=1)
    assert error_result["ok"] is False
    assert error_result["error_type"] == "PowerBIAPIError"
    assert "cannot be found" in error_result["context"]["body"]

    assert result.error is None
    assert result.dax_query == "EVALUATE ROW(\"x\", [Good Measure])"
    assert result.iterations[0].dax_ok is False
    assert result.iterations[1].dax_ok is True


def test_unknown_tool_name_returns_error_result_without_adapter_call() -> None:
    unknown = _ToolUseBlock("EVALUATE ROW(\"x\", 1)", name="run_sql")
    llm = FakeAnthropicClient([_tool_use(unknown), _end_turn("Sorry, tool confusion.")])
    powerbi = FakePowerBIClient()
    result = _make_orchestrator(llm, powerbi).run("What is x?")

    assert powerbi.executed == []
    body = _last_tool_result(llm)
    assert body["ok"] is False
    assert "run_sql" in body["error"]
    assert result.error is None


# ── Honest degradation ──────────────────────────────────────────────────


def test_max_iterations_returns_grace_answer_with_partial_data() -> None:
    # The model keeps calling the tool forever; the loop must give up after
    # _MAX_ITERATIONS while preserving the last successful data for debugging.
    responses = [
        _tool_use(_ToolUseBlock(f"EVALUATE ROW(\"x\", {i})")) for i in range(_MAX_ITERATIONS)
    ]
    results: list[QueryResult | Exception] = [
        _query_result([{"x": i}]) for i in range(_MAX_ITERATIONS)
    ]
    llm = FakeAnthropicClient(responses)
    result = _make_orchestrator(llm, FakePowerBIClient(results)).run("Loop forever")

    assert result.error_type == "MaxIterationsReached"
    assert result.answer  # honest, non-empty grace answer
    assert result.dax_query == f"EVALUATE ROW(\"x\", {_MAX_ITERATIONS - 1})"
    assert result.dax_rows == [{"x": _MAX_ITERATIONS - 1}]
    assert len(result.iterations) == _MAX_ITERATIONS


def test_anthropic_api_error_returns_grace_answer() -> None:
    api_error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.fake.invalid/v1/messages")
    )
    llm = FakeAnthropicClient([api_error])
    result = _make_orchestrator(llm, FakePowerBIClient()).run("Anything")

    assert result.error_type == "APIConnectionError"
    assert "try again" in result.answer.lower()
    # No invented numbers, no partial state.
    assert result.dax_rows is None


def test_unexpected_stop_reason_fails_closed() -> None:
    llm = FakeAnthropicClient([_Response("max_tokens", [_TextBlock("truncated...")])])
    result = _make_orchestrator(llm, FakePowerBIClient()).run("Anything")
    assert result.error_type == "UnexpectedStopReason"


# ── Context discipline ──────────────────────────────────────────────────


def test_tool_result_rows_are_capped_at_50() -> None:
    many_rows = [{"n": i} for i in range(120)]
    llm = FakeAnthropicClient(
        [_tool_use(_ToolUseBlock("EVALUATE big")), _end_turn("120 rows found.")]
    )
    powerbi = FakePowerBIClient([_query_result(many_rows)])
    _make_orchestrator(llm, powerbi).run("Everything please")

    body = _last_tool_result(llm)
    assert body["row_count"] == 120  # the true count survives...
    assert len(body["rows"]) == _MAX_ROWS_IN_TOOL_RESULT  # ...but only 50 rows travel


def test_history_role_filtering_and_date_context() -> None:
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "system", "content": "should be dropped"},
        {"role": "user", "content": ""},  # empty content dropped too
        {"role": "tool", "content": "should be dropped"},
    ]
    llm = FakeAnthropicClient([_end_turn("Done.")])
    _make_orchestrator(llm, FakePowerBIClient()).run("new question", conversation_history=history)

    messages = llm.messages.calls[0]["messages"]
    # Date context first (as a user message, never in system — cache rule),
    # then only the valid history pairs, then the new question.
    assert "Today's date" in messages[0]["content"]
    assert [m["role"] for m in messages[1:]] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "first question"
    assert messages[2]["content"] == "first answer"
    assert messages[3]["content"] == "new question"


# ── Suggestions extraction ──────────────────────────────────────────────


def test_extract_suggestions_variants_and_cap() -> None:
    text = (
        "The answer is 42.\n\n[next_step_suggestions]\n"
        "- First suggestion\n"
        "* Second suggestion.\n"
        "• Third suggestion\n"
        "- Fourth is beyond the cap\n"
    )
    answer, suggestions = _extract_suggestions(text)
    assert answer == "The answer is 42."
    assert suggestions == ["First suggestion", "Second suggestion", "Third suggestion"]


def test_extract_suggestions_missing_marker() -> None:
    answer, suggestions = _extract_suggestions("Just an answer.")
    assert answer == "Just an answer."
    assert suggestions == []


def test_extract_suggestions_strips_trailing_fence() -> None:
    text = "Answer text.\n```\n[next_step_suggestions]\n- Only one\n"
    answer, suggestions = _extract_suggestions(text)
    assert answer == "Answer text."
    assert suggestions == ["Only one"]


# ── System-block composition and debug payload ──────────────────────────


def test_extra_system_blocks_are_appended_after_the_cached_block() -> None:
    # Cache-prefix preservation: extras (e.g. a voice/formatting block) must
    # land AFTER the cache_control block, or every call re-writes the cache.
    llm = FakeAnthropicClient([_end_turn("ok")])
    orchestrator = _make_orchestrator(llm, FakePowerBIClient())
    extra = {"type": "text", "text": "Answer in exactly one sentence."}
    orchestrator.run("question", extra_system_blocks=[extra])

    system_arg = llm.messages.calls[0]["system"]
    assert len(system_arg) == 3  # header + cached body + extra
    assert system_arg[1].get("cache_control") == {"type": "ephemeral"}
    assert system_arg[-1]["text"] == "Answer in exactly one sentence."
    assert "cache_control" not in system_arg[-1]


def test_to_payload_includes_next_step_suggestions() -> None:
    result = OrchestratorResult(answer="ok", next_step_suggestions=["s1", "s2", "s3"])
    assert result.to_payload()["next_step_suggestions"] == ["s1", "s2", "s3"]


def test_to_payload_is_json_serializable() -> None:
    # The payload goes straight into the /chat response when debug=true —
    # a non-serializable field would 500 exactly when someone is debugging.
    result = OrchestratorResult(
        answer="ok",
        dax_query="EVALUATE ...",
        dax_rows=[{"k": "v"}],
        iterations=[
            IterationLog(
                iteration=1,
                latency_ms=100,
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=20,
                cache_write_tokens=0,
                cache_read_tokens=5,
            )
        ],
    )
    payload = result.to_payload()
    json.dumps(payload)
    assert payload["answer"] == "ok"
    assert payload["prompt_version"]
