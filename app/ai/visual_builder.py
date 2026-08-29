"""
Visual payload builder — turns DAX rows + the LLM's visual_hint into the
typed JSON contract the frontend renders.

Contract (the seam between LLM output and UI — keep frontend types in sync):

- kpi_cards:    {"visual_type": "kpi_cards", "title", "cards": [{"label", "value"}]}
- chart:        {"visual_type": "chart", "chart_type": "bar"|"line", "title",
                 "x_field", "y_field", "x_label", "y_label", "value_format",
                 "data": [{x, y}]}
- grouped bar:  {"visual_type": "chart", "chart_type": "grouped_bar", "title",
                 "x_field", "series": [...], "value_format", "data": [{x, s1, s2…}]}
- detail_table: {"visual_type": "detail_table", "title",
                 "columns": [{"key", "label", "value_format"?}],
                 "data": [...], "row_count"}

Production rules encoded here:
- The LLM's hint is primary; heuristics only fill gaps. A hint referencing
  columns that don't exist in the rows → skip WITH A LOGGED REASON (silent
  fallbacks hid a product-breaking regression once).
- Percent normalization: when value_format == "percent" and values are in
  (0, 1], multiply by 100 — the frontend renders the number it receives.
- Semantics travel IN the contract (value_format per visual/column), never
  by column-name sniffing in the frontend — that anti-pattern produced
  wrong currency symbols in production.
- Caps keep payloads sane: 24 bar points, 36 line points, 50×8 table.
"""
from __future__ import annotations

from typing import Any

_MAX_BAR_POINTS = 24
_MAX_LINE_POINTS = 36
_MAX_TABLE_ROWS = 50
_MAX_TABLE_COLS = 8
_MAX_KPI_CARDS = 6


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _humanize_key(key: str) -> str:
    """Fallback label: 'product_name' -> 'Product name'."""
    label = key.replace("_", " ").strip()
    return label[:1].upper() + label[1:] if label else key


def _log_skip(reason: str, detail: str = "") -> None:
    # The detail (row count, real column names, hint chart type) is what makes
    # a skip diagnosable from logs alone: "skipped" without the columns the
    # DAX actually returned sends you back to reproduce the query at 2am.
    suffix = f" {detail}" if detail else ""
    print(f"[visual] skipped reason={reason}{suffix}")


def _log_built(kind: str) -> None:
    print(f"[visual] built type={kind}")


def _normalize_percent(value: float, value_format: str | None) -> float:
    if value_format == "percent" and 0 < abs(value) <= 1:
        return value * 100
    return value


def _split_columns(rows: list[dict[str, Any]], columns: list[str]) -> tuple[list[str], list[str]]:
    """Partition result columns into categorical vs numeric by sampling rows."""
    categorical: list[str] = []
    numeric: list[str] = []
    for col in columns:
        sample = next((row[col] for row in rows if row.get(col) is not None), None)
        (numeric if _is_number(sample) else categorical).append(col)
    return categorical, numeric


def build_visual_payload(
    visual_hint: dict[str, Any] | None,
    rows: list[dict[str, Any]] | None,
    columns: list[str] | None,
) -> dict[str, Any] | None:
    """Build the typed visual payload, or None (prose-only answer)."""
    if not rows or not columns:
        _log_skip("no_dax_rows", f"hint={bool(visual_hint)}")
        return None
    if not visual_hint or not isinstance(visual_hint, dict):
        _log_skip("no_visual_hint_from_model", f"rows={len(rows)} cols={columns}")
        return None

    chart_type = visual_hint.get("chart_type")
    title = str(visual_hint.get("title") or "").strip() or None
    value_format = visual_hint.get("value_format")

    if chart_type in ("bar", "line"):
        # Production upgrade: a "bar" hint over rows carrying TWO categorical
        # dimensions would silently drop the second one and duplicate x
        # categories (one bar per underlying row). Detect and render grouped.
        categorical, _numeric = _split_columns(rows, columns)
        payload = None
        if chart_type == "bar" and len(categorical) >= 2:
            payload = _build_grouped_bar(visual_hint, rows, columns, title, value_format)
        if payload is None:
            payload = _build_chart(visual_hint, rows, columns, chart_type, title, value_format)
    elif chart_type == "grouped_bar":
        payload = _build_grouped_bar(visual_hint, rows, columns, title, value_format)
    elif chart_type == "table":
        payload = _build_table(rows, columns, title, value_format)
    elif chart_type == "kpi":
        payload = _build_kpi(rows, columns, title, value_format)
    else:
        _log_skip(f"unknown_chart_type:{chart_type}", f"cols={columns}")
        return None

    if payload is None:
        _log_skip(
            "builder_returned_none",
            f"hint_chart={chart_type} rows={len(rows)} cols={columns}",
        )
    else:
        _log_built(payload.get("chart_type") or payload["visual_type"])
    return payload


def _resolve_field(requested: str | None, candidates: list[str]) -> str | None:
    """
    Resolve a hint field name against the actual result columns.
    Exact match first; then case-insensitive; then leaf of 'table[col]'.
    """
    if not requested:
        return None
    if requested in candidates:
        return requested
    lower = requested.lower()
    for col in candidates:
        if col.lower() == lower:
            return col
    if "[" in requested and requested.endswith("]"):
        leaf = requested[requested.find("[") + 1 : -1]
        return _resolve_field(leaf, candidates)
    return None


def _build_chart(
    hint: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
    chart_type: str,
    title: str | None,
    value_format: str | None,
) -> dict[str, Any] | None:
    categorical, numeric = _split_columns(rows, columns)

    x_field = _resolve_field(hint.get("x_field"), columns) or (categorical[0] if categorical else None)
    y_field = _resolve_field(hint.get("y_field"), columns) or (numeric[0] if numeric else None)
    if not x_field or not y_field or x_field == y_field:
        print(f"[visual] field resolution failed: hint={hint.get('x_field')}/{hint.get('y_field')} cols={columns}")
        return None

    cap = _MAX_LINE_POINTS if chart_type == "line" else _MAX_BAR_POINTS
    data = []
    for row in rows[:cap]:
        y = row.get(y_field)
        # Null x would render a blank category label; drop the row instead.
        if row.get(x_field) is None or not _is_number(y):
            continue
        data.append({x_field: row.get(x_field), y_field: _normalize_percent(float(y), value_format)})
    if not data:
        return None

    return {
        "visual_type": "chart",
        "chart_type": chart_type,
        "title": title or _humanize_key(y_field),
        "x_field": x_field,
        "y_field": y_field,
        "x_label": _humanize_key(x_field),
        "y_label": _humanize_key(y_field),
        "value_format": value_format,
        "data": data,
    }


def _build_grouped_bar(
    hint: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
    title: str | None,
    value_format: str | None,
) -> dict[str, Any] | None:
    categorical, numeric = _split_columns(rows, columns)
    if len(categorical) < 2 or not numeric:
        return None

    x_field = _resolve_field(hint.get("x_field"), categorical) or categorical[0]
    series_field = next((c for c in categorical if c != x_field), None)
    y_field = _resolve_field(hint.get("y_field"), numeric) or numeric[0]
    if not series_field:
        return None

    series_values: list[str] = []
    grouped: dict[Any, dict[str, float]] = {}
    for row in rows:
        x = row.get(x_field)
        series_raw = row.get(series_field)
        y = row.get(y_field)
        # Null x would bucket as its own category and a null series would
        # become a literal "None" legend entry — drop those rows instead.
        if x is None or series_raw is None or not _is_number(y):
            continue
        series = str(series_raw)
        if series not in series_values:
            series_values.append(series)
        grouped.setdefault(x, {})[series] = _normalize_percent(float(y), value_format)

    data = []
    for x, values in list(grouped.items())[:_MAX_BAR_POINTS]:
        # Fill missing series combinations with 0.0 so bars align.
        entry: dict[str, Any] = {x_field: x}
        for series in series_values:
            entry[series] = values.get(series, 0.0)
        data.append(entry)
    if not data:
        return None

    return {
        "visual_type": "chart",
        "chart_type": "grouped_bar",
        "title": title or _humanize_key(y_field),
        "x_field": x_field,
        "series": series_values,
        "value_format": value_format,
        "data": data,
    }


def _build_table(
    rows: list[dict[str, Any]],
    columns: list[str],
    title: str | None,
    value_format: str | None,
) -> dict[str, Any] | None:
    kept_columns = columns[:_MAX_TABLE_COLS]
    _, numeric = _split_columns(rows, kept_columns)
    column_specs = []
    for col in kept_columns:
        spec: dict[str, Any] = {"key": col, "label": _humanize_key(col)}
        # The hint's value_format applies to numeric columns; categorical
        # columns carry none. Finer per-column typing can be added when the
        # model context declares it.
        if col in numeric and value_format:
            spec["value_format"] = value_format
        column_specs.append(spec)

    data = [
        {col: row.get(col) for col in kept_columns}
        for row in rows[:_MAX_TABLE_ROWS]
    ]
    return {
        "visual_type": "detail_table",
        "title": title,
        "columns": column_specs,
        "data": data,
        "row_count": len(rows),
    }


def _build_kpi(
    rows: list[dict[str, Any]],
    columns: list[str],
    title: str | None,
    value_format: str | None,
) -> dict[str, Any] | None:
    categorical, numeric = _split_columns(rows, columns)

    # Single row of numeric values → one card per column.
    if len(rows) == 1 and numeric:
        cards = [
            {
                "label": _humanize_key(col),
                "value": _normalize_percent(float(rows[0][col]), value_format),
            }
            for col in numeric
            if _is_number(rows[0].get(col))
        ][:_MAX_KPI_CARDS]
        if cards:
            return {"visual_type": "kpi_cards", "title": title, "cards": cards}

    # Few rows of one categorical + one numeric → one card per row.
    if 1 < len(rows) <= _MAX_KPI_CARDS and len(categorical) == 1 and len(numeric) == 1:
        cards = []
        for row in rows:
            value = row.get(numeric[0])
            label = row.get(categorical[0])
            # A null label would print a literal "None" card title.
            if label is not None and _is_number(value):
                cards.append(
                    {
                        "label": str(label),
                        "value": _normalize_percent(float(value), value_format),
                    }
                )
        if cards:
            return {"visual_type": "kpi_cards", "title": title, "cards": cards}

    return None
