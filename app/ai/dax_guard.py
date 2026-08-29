"""
Structural pre-flight validation for generated DAX.

The guard's error messages are INSTRUCTIVE on purpose: they are fed back to
the model through the tool result, so the normal retry loop self-corrects
without spending a Power BI round trip. Fix generation defects at both ends —
the prompt that teaches and the validator that enforces.

Extend `validate_dax_query` with rules specific to YOUR model (Mission M23).
"""
from __future__ import annotations

import re

# A table constructor `{ ... }` used directly as the top-level EVALUATE
# expression returns anonymous columns ([Value1], [Value2], ...), which then
# surface as raw labels in KPI cards and charts. Rule: name columns
# explicitly with ROW("Name", expr) / SELECTCOLUMNS / SUMMARIZECOLUMNS.
# The pattern only matches `{` immediately after the EVALUATE keyword, so
# `... IN { ... }` filters and `{}` nested inside functions are untouched.
_EVALUATE_TABLE_CONSTRUCTOR_RE = re.compile(r"\bEVALUATE\b\s*\{", re.IGNORECASE)


def validate_dax_query(dax: str) -> str | None:
    """
    Return an instructive error message when the query uses a forbidden
    pattern, or None when it is acceptable.
    """
    if _EVALUATE_TABLE_CONSTRUCTOR_RE.search(dax):
        return (
            "DAX rejected: the EVALUATE uses a table constructor `{ ... }`, "
            "which returns anonymous columns (Value1, Value2) and produces raw "
            "labels in visuals. Replace it with ROW(\"Name\", expr) using "
            'explicit column names — e.g. EVALUATE ROW("Revenue May 2026", '
            "CALCULATE([Revenue], ...)). For several values use ROW with "
            'multiple "Name", expr pairs (or SELECTCOLUMNS/SUMMARIZECOLUMNS '
            "with names). Filters using IN { ... } are valid and need no change."
        )
    return None
