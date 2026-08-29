"""
DAX conversation orchestrator — the heart of the AI Data Analyst.

Encapsulates the multi-iteration loop between the LLM (DAX generation) and
Power BI (DAX execution):

    question → LLM → execute_dax tool → guard → Power BI → rows/error → LLM
             → (self-correct on error, up to the iteration cap) → answer

Design rules validated in production:
- STATELESS: no DB access here; the caller fetches history and passes it in.
  A fresh orchestrator is built per request.
- Errors are FOOD, not failures: guard rejections and Power BI error bodies
  go back to the model as tool results with is_error=True, and the model
  corrects itself. Four DAX errors in a production stress test — all
  self-recovered, zero visible to users.
- Rows in tool results are capped (context discipline).
- Every iteration is logged with latency + token/cache counters: cache
  behavior is a load-bearing cost decision and must stay observable.
- The model NEVER invents numbers: exhaustion and API errors return honest
  failure answers, with any partial DAX data preserved for debugging.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app.adapters.powerbi import (
    PowerBIAPIError,
    PowerBIAuthError,
    PowerBIClient,
    PowerBIConfigError,
)
from app.ai.dax_guard import validate_dax_query
from app.ai.prompts import (
    EXECUTE_DAX_TOOL,
    PROMPT_VERSION,
    build_date_context_message,
    build_system_prompt_blocks,
    get_max_retries,
)

# Hard cap on loop iterations: initial call + N retries + final response.
_MAX_ITERATIONS = get_max_retries() + 2

# Cap rows in tool_result to bound the context the model re-reads each turn.
_MAX_ROWS_IN_TOOL_RESULT = 50

_SUGGESTIONS_MARKER = "[next_step_suggestions]"


def _extract_suggestions(text: str) -> tuple[str, list[str]]:
    """
    Extract the [next_step_suggestions] block from the answer text.

    Returns (cleaned_answer, suggestions). Bullets may use "-", "*", or "•";
    at most 3 suggestions are kept. Missing marker → (text, []).
    """
    if _SUGGESTIONS_MARKER not in text:
        return text.strip(), []

    parts = text.split(_SUGGESTIONS_MARKER, 1)
    cleaned_answer = parts[0].rstrip()
    suggestions_block = parts[1] if len(parts) > 1 else ""

    suggestions: list[str] = []
    for line in suggestions_block.splitlines():
        match = re.match(r"^[-*•]\s+(.+)$", line.strip())
        if match:
            suggestion = match.group(1).strip().rstrip(".;,")
            if suggestion:
                suggestions.append(suggestion)
        if len(suggestions) >= 3:
            break

    # Strip trailing markdown fences the model may wrap around the block.
    cleaned_answer = re.sub(r"\n*```\s*$", "", cleaned_answer).rstrip()
    return cleaned_answer, suggestions


@dataclass
class IterationLog:
    """One iteration of the LLM↔Power BI loop, for observability."""

    iteration: int
    latency_ms: int
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    dax_executed: str | None = None
    dax_explanation: str | None = None
    dax_ok: bool | None = None
    dax_error: str | None = None
    dax_row_count: int | None = None
    dax_latency_ms: int | None = None
    pre_tool_text: str | None = None


@dataclass
class OrchestratorResult:
    """Structured result of a full conversation turn."""

    answer: str
    visual_hint: dict[str, Any] | None = None
    next_step_suggestions: list[str] = field(default_factory=list)
    dax_query: str | None = None
    dax_rows: list[dict[str, Any]] | None = None
    dax_columns: list[str] | None = None
    iterations: list[IterationLog] = field(default_factory=list)
    total_latency_ms: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    prompt_version: str = PROMPT_VERSION
    error: str | None = None
    error_type: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "visual_hint": self.visual_hint,
            "next_step_suggestions": self.next_step_suggestions,
            "dax_query": self.dax_query,
            "dax_rows": self.dax_rows,
            "dax_columns": self.dax_columns,
            "iterations": [vars(it) for it in self.iterations],
            "total_latency_ms": self.total_latency_ms,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_write_tokens": self.total_cache_write_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "prompt_version": self.prompt_version,
            "error": self.error,
            "error_type": self.error_type,
        }


class DAXConversationOrchestrator:
    """Orchestrates the LLM ↔ Power BI loop for a single conversation turn."""

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        powerbi_client: PowerBIClient,
        dataset_id: str,
        model: str = "claude-sonnet-4-5",
        max_tokens: int = 4096,
    ) -> None:
        if not dataset_id or not dataset_id.strip():
            raise ValueError("dataset_id is required")
        self._anthropic = anthropic_client
        self._powerbi = powerbi_client
        self._dataset_id = dataset_id.strip()
        self._model = model
        self._max_tokens = max_tokens

    def run(
        self,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
        extra_system_blocks: list[dict[str, Any]] | None = None,
    ) -> OrchestratorResult:
        """
        Run a full conversation turn.

        Args:
            user_message: the user's question.
            conversation_history: prior {role, content} messages (excluding
                the new user_message). May be empty.
            extra_system_blocks: optional additional system blocks (e.g. a
                voice formatting block). NOT cached — keep them small.
        """
        if not user_message or not user_message.strip():
            return OrchestratorResult(
                answer="Empty question.",
                error="empty_user_message",
                error_type="ValueError",
            )

        system_blocks = build_system_prompt_blocks()
        if extra_system_blocks:
            system_blocks = system_blocks + list(extra_system_blocks)

        # Date context travels as a message, NOT in the system prompt —
        # otherwise the changing date would invalidate the prompt cache.
        messages: list[dict[str, Any]] = [build_date_context_message()]
        if conversation_history:
            for msg in conversation_history:
                if msg.get("role") in {"user", "assistant"} and msg.get("content"):
                    messages.append({"role": msg["role"], "content": str(msg["content"])})
        messages.append({"role": "user", "content": user_message.strip()})

        result = OrchestratorResult(answer="", prompt_version=PROMPT_VERSION)
        last_dax_query: str | None = None
        last_dax_rows: list[dict[str, Any]] | None = None
        last_dax_columns: list[str] | None = None
        last_visual_hint: dict[str, Any] | None = None

        for iteration in range(_MAX_ITERATIONS):
            start = time.monotonic()
            try:
                response = self._anthropic.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=system_blocks,
                    tools=[EXECUTE_DAX_TOOL],
                    messages=messages,
                )
            except anthropic.APIError as exc:
                result.error = str(exc)
                result.error_type = type(exc).__name__
                result.answer = "Temporary error contacting the AI service. Please try again."
                result.total_latency_ms += int((time.monotonic() - start) * 1000)
                return result

            elapsed = int((time.monotonic() - start) * 1000)
            usage = response.usage
            log = IterationLog(
                iteration=iteration + 1,
                latency_ms=elapsed,
                stop_reason=response.stop_reason or "unknown",
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            )
            print(
                f"[llm] elapsed: {elapsed}ms (iteration {iteration + 1}) | "
                f"in={log.input_tokens} cache_read={log.cache_read_tokens} "
                f"cache_write={log.cache_write_tokens} out={log.output_tokens}"
            )
            result.total_latency_ms += elapsed
            result.total_cache_read_tokens += log.cache_read_tokens
            result.total_cache_write_tokens += log.cache_write_tokens
            result.total_input_tokens += log.input_tokens
            result.total_output_tokens += log.output_tokens

            # Branch 1: end_turn — final answer
            if response.stop_reason == "end_turn":
                text_blocks = [b.text for b in response.content if b.type == "text"]
                cleaned_answer, suggestions = _extract_suggestions(
                    "\n".join(text_blocks).strip()
                )
                result.answer = cleaned_answer
                result.next_step_suggestions = suggestions
                result.iterations.append(log)
                result.dax_query = last_dax_query
                result.dax_rows = last_dax_rows
                result.dax_columns = last_dax_columns
                result.visual_hint = last_visual_hint
                return result

            # Branch 2: tool_use — execute DAX
            if response.stop_reason == "tool_use":
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                text_blocks = [b.text for b in response.content if b.type == "text"]
                if text_blocks:
                    log.pre_tool_text = text_blocks[0][:500]

                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for tu in tool_uses:
                    if tu.name != "execute_dax":
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": json.dumps(
                                    {"ok": False, "error": f"unknown tool {tu.name}"}
                                ),
                                "is_error": True,
                            }
                        )
                        continue

                    dax = tu.input.get("query", "")
                    explanation = tu.input.get("explanation", "")
                    visual_hint = tu.input.get("visual_hint")

                    log.dax_executed = dax[:1000]
                    log.dax_explanation = explanation[:500]

                    exec_result = self._execute_dax_safely(dax)
                    log.dax_ok = exec_result.get("ok")
                    log.dax_row_count = exec_result.get("row_count")
                    log.dax_latency_ms = exec_result.get("latency_ms")
                    if not exec_result.get("ok"):
                        log.dax_error = exec_result.get("error_message", "")[:500]

                    if exec_result.get("ok"):
                        last_dax_query = dax
                        last_dax_rows = exec_result.get("rows")
                        last_dax_columns = exec_result.get("columns")
                        last_visual_hint = visual_hint

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": json.dumps(exec_result, ensure_ascii=False, default=str),
                            "is_error": not exec_result.get("ok"),
                        }
                    )

                messages.append({"role": "user", "content": tool_results})
                result.iterations.append(log)
                continue

            # Branch 3: unexpected stop reason
            result.error = f"unexpected stop_reason: {response.stop_reason}"
            result.error_type = "UnexpectedStopReason"
            result.answer = "Unexpected error in the generation flow."
            result.iterations.append(log)
            return result

        # Exhausted the iteration budget without a final answer. Be honest —
        # never invent numbers the queries didn't return.
        result.error = f"max iterations reached ({_MAX_ITERATIONS})"
        result.error_type = "MaxIterationsReached"
        result.answer = (
            "I couldn't reach a reliable answer within the attempt limit. "
            "Try rephrasing the question, or ask again."
        )
        result.dax_query = last_dax_query
        result.dax_rows = last_dax_rows
        result.dax_columns = last_dax_columns
        result.visual_hint = last_visual_hint
        return result

    def _execute_dax_safely(self, dax: str) -> dict[str, Any]:
        """
        Execute DAX and return a JSON-serializable dict for the tool result.

        On success: {ok: True, row_count, columns, rows[:cap], latency_ms}
        On failure: {ok: False, error_type, error_message, context}
        """
        if not dax or not dax.strip():
            return {
                "ok": False,
                "error_type": "ValueError",
                "error_message": "DAX query is empty",
                "context": {},
            }
        validation_error = validate_dax_query(dax)
        if validation_error:
            # Guard rejection: no Power BI round trip, instructive message
            # goes straight back to the model.
            return {
                "ok": False,
                "error_type": "DaxValidationError",
                "error_message": validation_error,
                "context": {"guard": "evaluate_table_constructor"},
            }
        try:
            result = self._powerbi.execute_query(self._dataset_id, dax)
            return {
                "ok": True,
                "row_count": result.row_count,
                "columns": result.columns,
                "rows": result.rows[:_MAX_ROWS_IN_TOOL_RESULT],
                "latency_ms": result.metadata.get("latency_ms"),
            }
        except (PowerBIAPIError, PowerBIAuthError, PowerBIConfigError) as exc:
            return {
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "context": getattr(exc, "context", {}) or {},
            }
