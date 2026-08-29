"""Unit tests for `app.ai.prompts` — the prompt-caching contract.

These tests pin the static contract of the module: the two-block system
prompt with `cache_control` on the LARGE block only, the tool definition
shape, and the date-travels-as-user-message rule. They do NOT call the
Anthropic API.

Why this matters: the ~25x cost reduction depends on cache placement. A
per-request value (the date!) leaking into the cached block, or
`cache_control` landing on the wrong block, silently costs the full prompt
price on every turn — no error, just a bigger invoice.
"""
from __future__ import annotations

import importlib
import re
from datetime import UTC, datetime

import pytest

from app.ai import prompts

# ── Block structure and cache placement ──────────────────────────────────


def test_build_system_prompt_blocks_returns_two_blocks() -> None:
    blocks = prompts.build_system_prompt_blocks()
    assert isinstance(blocks, list)
    assert len(blocks) == 2


def test_second_block_has_cache_control() -> None:
    blocks = prompts.build_system_prompt_blocks()
    assert blocks[1].get("cache_control") == {"type": "ephemeral"}


def test_first_block_has_no_cache_control() -> None:
    blocks = prompts.build_system_prompt_blocks()
    assert "cache_control" not in blocks[0]


def test_first_block_contains_prompt_version() -> None:
    blocks = prompts.build_system_prompt_blocks()
    assert prompts.PROMPT_VERSION in blocks[0]["text"]


def test_prompt_version_constant_format() -> None:
    assert isinstance(prompts.PROMPT_VERSION, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", prompts.PROMPT_VERSION)


def test_no_current_date_in_any_system_block() -> None:
    # The hard rule behind build_date_context_message(): a date in the cached
    # block invalidates the cache daily; a date anywhere in `system` is a
    # smell. Today's date must never appear in either block.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    for block in prompts.build_system_prompt_blocks():
        assert today not in block["text"]


def test_build_system_prompt_length_is_reasonable() -> None:
    total = sum(len(b["text"]) for b in prompts.build_system_prompt_blocks())
    assert 5_000 <= total <= 250_000, (
        f"prompt length {total} chars outside expected envelope — an "
        f"under-filled context pack starves the analyst; an oversized one "
        f"blows the cache budget"
    )


# ── Prompt content markers ───────────────────────────────────────────────


def test_second_block_includes_expected_sections() -> None:
    body = prompts.build_system_prompt_blocks()[1]["text"]
    for marker in (
        "Semantic model context",
        "Operational rules",
        "EVALUATE",
        "MANDATORY RULES",
    ):
        assert marker in body, f"expected '{marker}' in cached block"


def test_second_block_contains_examples_section_if_loaded() -> None:
    body = prompts.build_system_prompt_blocks()[1]["text"]
    if prompts._DAX_EXAMPLES:
        assert "Validated DAX examples" in body


def test_system_prompt_mentions_next_step_suggestions_marker() -> None:
    body = prompts.build_system_prompt_blocks()[1]["text"]
    assert "[next_step_suggestions]" in body


def test_system_prompt_requires_3_suggestions() -> None:
    body = prompts.build_system_prompt_blocks()[1]["text"].lower()
    assert re.search(r"3\s+suggest", body)


def test_system_prompt_has_visual_hint_rule() -> None:
    body = prompts.build_system_prompt_blocks()[1]["text"]
    assert "visual_hint" in body
    # The rule must map answer shapes to chart types explicitly.
    assert "Ranking" in body
    assert "grouped_bar" in body


# ── Date context message ─────────────────────────────────────────────────


def test_build_date_context_message_returns_user_role() -> None:
    msg = prompts.build_date_context_message()
    assert msg["role"] == "user"
    assert isinstance(msg["content"], str)
    assert "Today's date" in msg["content"]
    assert "Current month" in msg["content"]


# ── Context pack loading ─────────────────────────────────────────────────


def test_load_context_pack_raises_if_files_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # A half-configured analyst must not boot.
    monkeypatch.setattr(prompts, "_PACK_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        prompts._load_context_pack()


def test_load_dax_examples_returns_string() -> None:
    assert isinstance(prompts._load_dax_examples(), str)


def test_module_reimport_loads_context_at_import_time() -> None:
    # Reimport and confirm _MODEL_CONTEXT is populated eagerly (not lazy).
    reloaded = importlib.reload(prompts)
    assert isinstance(reloaded._MODEL_CONTEXT, str)
    assert reloaded._MODEL_CONTEXT.strip()


def test_dax_examples_carry_visual_hints_but_never_kpi() -> None:
    # The few-shots are what the model learns visual_hint usage from: keep at
    # least one charted example when you author your own pack. One data
    # representation per response: single values live in prose, so no example
    # may carry a kpi hint.
    examples = prompts._DAX_EXAMPLES
    if examples:
        assert '"chart_type"' in examples, (
            "EXAMPLES.md should include at least one visual_hint example"
        )
        assert '"chart_type": "kpi"' not in examples


# ── Tool definition ──────────────────────────────────────────────────────


def test_execute_dax_tool_has_required_top_level_fields() -> None:
    tool = prompts.EXECUTE_DAX_TOOL
    assert tool["name"] == "execute_dax"
    assert isinstance(tool["description"], str) and tool["description"].strip()
    assert isinstance(tool["input_schema"], dict)


def test_execute_dax_tool_schema_requires_query_and_explanation() -> None:
    schema = prompts.EXECUTE_DAX_TOOL["input_schema"]
    assert schema.get("type") == "object"

    properties = schema.get("properties", {})
    assert properties["query"].get("type") == "string"
    assert properties["explanation"].get("type") == "string"

    assert {"query", "explanation"}.issubset(set(schema.get("required", [])))


def test_execute_dax_tool_supports_optional_visual_hint() -> None:
    schema = prompts.EXECUTE_DAX_TOOL["input_schema"]
    vh = schema["properties"]["visual_hint"]
    assert vh.get("type") == "object"

    # visual_hint must NOT be required (optional by design).
    assert "visual_hint" not in set(schema.get("required", []))

    vh_props = vh.get("properties", {})
    for field in ("chart_type", "x_field", "y_field", "title", "value_format"):
        assert field in vh_props

    chart_types = vh_props["chart_type"].get("enum", [])
    for chart in ("bar", "line", "kpi", "table", "grouped_bar"):
        assert chart in chart_types

    value_formats = vh_props["value_format"].get("enum", [])
    for fmt in ("currency", "percent", "integer", "decimal"):
        assert fmt in value_formats


def test_execute_dax_tool_description_guides_visual_by_shape() -> None:
    # The description steers by answer shape (ranking -> bar, series -> line)
    # and deliberately does NOT pitch a visual for single values.
    desc = prompts.EXECUTE_DAX_TOOL["description"]
    for token in ("bar", "line", "table", "grouped_bar"):
        assert token in desc, f"tool description must mention '{token}'"


# ── Loop configuration ───────────────────────────────────────────────────


def test_get_max_retries_returns_four() -> None:
    assert prompts.get_max_retries() == 4


def test_get_conversation_window_returns_twelve() -> None:
    assert prompts.get_conversation_window() == 12
