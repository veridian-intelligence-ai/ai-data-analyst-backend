"""
Unit tests for the DAX pre-flight guard (app/ai/dax_guard.py).

The guard exists to catch ONE structural defect cheaply: a bare table
constructor after EVALUATE, whose anonymous [Value1]/[Value2] columns leak
raw labels into visuals. Everything else — including `IN { ... }` filters —
must pass untouched, and the rejection message must teach the fix (it is fed
back to the model, not to a human).
"""
from __future__ import annotations

import pytest

from app.ai.dax_guard import validate_dax_query

# ── Rejected: EVALUATE { ... } table constructors ───────────────────────


@pytest.mark.parametrize(
    "dax",
    [
        "EVALUATE {CALCULATE([Revenue])}",
        "EVALUATE { 1 + 1 }",
        "evaluate {[Revenue]}",  # case-insensitive
        "Evaluate    {  [Revenue]  }",  # arbitrary whitespace
        "EVALUATE\n{\n    [Revenue]\n}",  # newline between keyword and brace
        "EVALUATE\t{ [Revenue] }",  # tab
    ],
)
def test_rejects_evaluate_table_constructor(dax: str) -> None:
    assert validate_dax_query(dax) is not None


# ── Allowed: named-column constructs and IN { ... } filters ─────────────


@pytest.mark.parametrize(
    "dax",
    [
        'EVALUATE ROW("Revenue", CALCULATE([Total Revenue]))',
        'EVALUATE SELECTCOLUMNS(products, "Name", products[name])',
        'EVALUATE SUMMARIZECOLUMNS(products[category], "Revenue", [Total Revenue])',
        # IN { ... } filters are set membership, not a top-level constructor.
        (
            "EVALUATE SUMMARIZECOLUMNS(products[category], "
            'FILTER(ALL(calendar), calendar[month] IN { "2026-01", "2026-02" }), '
            '"Revenue", [Total Revenue])'
        ),
        # Braces nested inside a function argument are also fine.
        'EVALUATE CALCULATETABLE(VALUES(products[name]), products[category] IN { "A" })',
    ],
)
def test_allows_named_columns_and_in_filters(dax: str) -> None:
    assert validate_dax_query(dax) is None


# ── The message must be instructive ─────────────────────────────────────


def test_rejection_message_teaches_the_fix() -> None:
    message = validate_dax_query("EVALUATE {[Revenue]}")
    assert message is not None
    # Names the defect...
    assert "anonymous columns" in message
    # ...shows the replacement pattern...
    assert "ROW(" in message
    assert "SELECTCOLUMNS" in message
    # ...and explicitly whitelists the pattern that IS allowed, so the model
    # doesn't over-correct and stop using IN filters.
    assert "IN { ... }" in message
