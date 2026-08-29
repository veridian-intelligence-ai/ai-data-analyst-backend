"""
System prompt assembly + tool definition for NL→DAX generation.

The architecture (validated in production, ~25× cost reduction, 95% cache
hit rate):

1. TWO system blocks: a small uncached header + one LARGE cached block
   (`cache_control: ephemeral`) carrying the semantic-model context pack and
   the operational rules. The big block only changes when your context pack
   changes — which is exactly when the cache SHOULD invalidate.
2. The current date is injected as a prepended USER MESSAGE, never into the
   system prompt: a date in the cached block would invalidate the cache
   every day (and a date in an uncached system block would still re-order
   blocks). See build_date_context_message().
3. PROMPT_VERSION stamps every response payload so you can correlate answer
   quality with prompt changes in production.
4. The model context lives in three EXTERNAL files (the "context pack"):
   business layer, schema layer, examples layer. You author them for YOUR
   model in Missions M06–M07. Missing files fail at import — a half-
   configured analyst must not boot.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROMPT_VERSION = "1.0.0"

_PACK_DIR = Path(__file__).parent.parent / "context_pack"


def _load_context_pack() -> str:
    """Load and concatenate the semantic-model context pack (fail-fast)."""
    business = (_PACK_DIR / "BUSINESS.md").read_text(encoding="utf-8")
    schema = (_PACK_DIR / "SCHEMA.md").read_text(encoding="utf-8")
    return f"{business}\n\n---\n\n{schema}"


def _load_dax_examples() -> str:
    """Load few-shot DAX examples. Returns empty string if the file is missing."""
    path = _PACK_DIR / "EXAMPLES.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


_MODEL_CONTEXT = _load_context_pack()
_DAX_EXAMPLES = _load_dax_examples()


def _dataset_display_name() -> str:
    return os.getenv("DATASET_DISPLAY_NAME", "the semantic model").strip() or "the semantic model"


def build_system_prompt_blocks() -> list[dict[str, Any]]:
    """
    Build the system prompt as content blocks with prompt caching enabled on
    the large static portion.

    Returns exactly 2 blocks for the `system` param of messages.create():
    - Block 1: header (small, not cached)
    - Block 2: model context + examples + rules (LARGE, cached)
    """
    model_name = _dataset_display_name()

    header = (
        f"You are a senior data analyst working with the Power BI semantic "
        f"model {model_name}. You always answer in the language of the "
        f"user's last message, with a direct, professional tone.\n\n"
        f"Prompt version: {PROMPT_VERSION}\n\n"
        f"Your job: receive business questions and generate correct DAX "
        f"queries that answer them against the model. You have a tool "
        f"`execute_dax(query, explanation)` that runs DAX against the model. "
        f"Use it whenever you need data to answer."
    )

    examples_section = (
        f"\n\n---\n\n# Validated DAX examples\n\n{_DAX_EXAMPLES}" if _DAX_EXAMPLES else ""
    )

    static_body = f"""---

# Semantic model context

The context below was authored for THIS model. Its MANDATORY RULES section
(grain contract, default filters, naming traps) has absolute priority: every
query you generate must respect every mandatory rule, without exceptions,
unless the user explicitly asks otherwise.

{_MODEL_CONTEXT}{examples_section}

---

# Operational rules

1. **Before generating DAX**, decide clearly:
   - What question the user is actually asking (restate it precisely if needed)
   - Which measure(s) answer it
   - Which axis/axes are needed
   - Which temporal or categorical filters apply
   - Is the question answerable at the model's grain? If not, answer with the
     model's standard refusal sentence from the context pack — do not
     approximate silently.

2. **Generating DAX**:
   - Always start with `EVALUATE`
   - Prefer `SUMMARIZECOLUMNS` for aggregations with axes
   - For top-N use the `TOPN + ADDCOLUMNS + VALUES + CALCULATE` pattern
     (see the validated examples)
   - For temporal filters inside SUMMARIZECOLUMNS, use
     `FILTER(ALL(<calendar table>), ...)`
   - For single values use `ROW("Name", value)` with an explicit column name
   - Never reference technical/hidden columns directly when a measure exists
   - Respect the naming conventions documented in the context pack

3. **Validate mentally before executing**:
   - Are you using the correct date key for joins (see the schema layer)?
   - Are display axes human-friendly (names, not technical codes)?
   - Is the question valid at the model's grain?
   - Did you apply EVERY mandatory rule from the context pack?

4. **If the query fails** (Power BI error), you receive the error message.
   Analyze it, correct the query, and try again. Maximum 2 retries per
   question. If it still fails on the third attempt, explain to the user
   what went wrong — NEVER invent data. No number may appear in your answer
   that did not come from returned query rows; if queries fail, say so.

5. **Formatting the answer**:
   - ALWAYS answer in the language of the user's last message (this rule
     also applies to suggestions and visual titles). If the last message is
     ambiguous, follow the language of the previous conversation turns.
   - For single values: include the temporal context and the metric name
   - For lists: state the period and the ordering
   - For comparisons: give both the % difference and the absolute difference
   - **One representation of the data per answer:** when you emit a
     `visual_hint` (`bar`/`line`/`grouped_bar`/`table`), the text is a short
     narrative — context, reading, 1-2 key numbers — and does NOT reproduce
     the full table in markdown or repeat the whole series; that is the
     visual's job. Without a `visual_hint` (pure prose, single value,
     compact comparison) → normal formatting: markdown tables and bold
     highlights are welcome.
   - Do not recite the DAX to the user unless asked
   - At the end of your final answer, ALWAYS include a block with 3
     suggested follow-up questions, in exactly this format:

     [next_step_suggestions]
     - {{suggested question 1}}
     - {{suggested question 2}}
     - {{suggested question 3}}

     Suggestions must: be short (max 8 words each); be answerable with this
     model at its grain; vary the analytical angle; deepen or extend the
     answer just given; be in the same language as the answer.

     EXCEPTION: do NOT include the block in voice mode (see the VOICE MODE
     block if present).

6. **When the user is ambiguous**:
   - If the ambiguity is resolvable from the context pack's glossary or
     measure descriptions, resolve it silently.
   - If the ambiguity is structural (e.g. "performance" with no context),
     ask ONE short clarifying question.

7. **Composite analyses**: when the user asks for an analytical technique
   (Pareto, distribution, segmentation, complex temporal comparison), you
   already know what it is. Compose it from the model's measures and tables
   without explaining the technique.

8. **Suggesting a visualization** (the `visual_hint` parameter of
   `execute_dax`). Principle: ONE representation of the data per answer.
   Choose by the shape of the answer:
   - **Ranking / top N / categorical comparison** → `"bar"`
   - **Series / temporal evolution** (months, quarters, years axis) → `"line"`
   - **Two categorical dimensions + one measure** → `"grouped_bar"`
   - **Detail with several columns and many rows** → `"table"`
   - **Single value** → NO `visual_hint`; the number lives in the prose,
     highlighted.
   - **Compact comparison of a few periods** → NO `visual_hint`; present it
     as a markdown table in the text.
   - **Pure prose** (a clarifying question, a refusal because the model's
     grain cannot answer, an explanation without new data) → NO
     `visual_hint`. If an earlier tool call in this turn carried one, the
     final prose-only answer must still omit it.

   When you emit a `visual_hint`, fill `x_field` (the dimension/axis),
   `y_field` (the measure), `title` (in the answer's language) and
   `value_format` (`currency` for money, `percent` for ratios/rates,
   `integer` or `decimal` for counts). The names in `x_field`/`y_field`
   must be EXACTLY the column names returned by the query.

---

Ready. Wait for the user's question."""

    return [
        {"type": "text", "text": header},
        {
            "type": "text",
            "text": static_body,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def build_date_context_message() -> dict[str, str]:
    """
    Build a user-role message carrying the current date context.

    Prepended to `messages` (NOT placed in `system`) so the changing date
    never invalidates the cached system prompt.
    """
    tz_name = os.getenv("APP_TIMEZONE", "UTC")
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now(UTC)
    return {
        "role": "user",
        "content": (
            f"[context] Today's date: {now.strftime('%Y-%m-%d')} "
            f"({now.strftime('%A')}). Local time: {now.strftime('%H:%M')} "
            f"({tz_name}). Current month: {now.strftime('%Y%m')} "
            f"({now.strftime('%B %Y')})."
        ),
    }


# Tool definition for the Anthropic API.
EXECUTE_DAX_TOOL: dict[str, Any] = {
    "name": "execute_dax",
    "description": (
        "Executes a DAX query against the Power BI semantic model. Returns a "
        "tabular result (columns + rows). On DAX errors (syntax, unknown "
        "column, etc.) it returns Power BI's error message; you can correct "
        "the query and try again. You may optionally suggest a visual_hint "
        "so the frontend renders an appropriate chart (bar, line, table, "
        "grouped_bar)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Complete DAX query starting with EVALUATE. Do not include "
                    "// or /* */ comments — Power BI ignores them."
                ),
            },
            "explanation": {
                "type": "string",
                "description": (
                    "Short explanation (1-2 sentences) of what this query "
                    "answers and which measures/axes it uses. For audit and "
                    "debugging."
                ),
            },
            "visual_hint": {
                "type": "object",
                "description": (
                    "Visualization suggestion for the frontend. Principle: one "
                    "single representation of the data per answer — the data "
                    "goes EITHER in the visual OR in the text, never both. "
                    "Choose chart_type by the answer's shape: ranking/top N/"
                    "categorical comparison -> bar; temporal evolution -> line; "
                    "two dimensions + measure -> grouped_bar; wide detail with "
                    "many rows -> table. Do NOT emit visual_hint for a single "
                    "value (the number goes in the prose, highlighted) nor for "
                    "compact comparisons of a few periods (markdown table in "
                    "the text). Fill x_field, y_field, title and value_format "
                    "(column names exactly as returned by the query)."
                ),
                "properties": {
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "line", "kpi", "table", "grouped_bar"],
                    },
                    "x_field": {"type": "string"},
                    "y_field": {"type": "string"},
                    "title": {"type": "string"},
                    "value_format": {
                        "type": "string",
                        "enum": ["currency", "percent", "integer", "decimal"],
                    },
                },
            },
        },
        "required": ["query", "explanation"],
    },
}


def get_max_retries() -> int:
    """Maximum DAX execution retries before giving up."""
    return 4


def get_conversation_window() -> int:
    """Number of past messages to include as conversation history."""
    return 12
