"""
Unit tests for the visual payload builder (app/ai/visual_builder.py).

The builder is the seam between LLM output and the frontend's typed renderer.
The suite pins the contract's load-bearing rules: skip (return None) instead
of guessing, resolve hint fields tolerantly (exact → case-insensitive →
'table[col]' leaf), normalize percent values ×100 only when they arrive as
ratios in (0, 1], fill missing grouped-bar combos with 0.0, and keep payload
caps (50×8 table) while reporting the REAL row_count.
"""
from __future__ import annotations

from typing import Any

from app.ai.visual_builder import build_visual_payload

_BAR_ROWS: list[dict[str, Any]] = [
    {"category": "Hardware", "revenue": 1200.0},
    {"category": "Software", "revenue": 800.0},
    {"category": "Services", "revenue": 450.0},
]
_BAR_COLUMNS = ["category", "revenue"]


def _bar_hint(**overrides: Any) -> dict[str, Any]:
    hint = {
        "chart_type": "bar",
        "x_field": "category",
        "y_field": "revenue",
        "title": "Revenue by category",
        "value_format": "currency",
    }
    hint.update(overrides)
    return hint


# ── Skip rules ──────────────────────────────────────────────────────────


def test_no_rows_returns_none() -> None:
    assert build_visual_payload(_bar_hint(), [], _BAR_COLUMNS) is None
    assert build_visual_payload(_bar_hint(), None, _BAR_COLUMNS) is None


def test_no_hint_returns_none() -> None:
    assert build_visual_payload(None, _BAR_ROWS, _BAR_COLUMNS) is None
    assert build_visual_payload("bar", _BAR_ROWS, _BAR_COLUMNS) is None  # type: ignore[arg-type]


def test_unknown_chart_type_returns_none() -> None:
    assert build_visual_payload(_bar_hint(chart_type="pie"), _BAR_ROWS, _BAR_COLUMNS) is None


# ── Bar chart + field resolution ────────────────────────────────────────


def test_bar_chart_happy_path() -> None:
    payload = build_visual_payload(_bar_hint(), _BAR_ROWS, _BAR_COLUMNS)
    assert payload is not None
    assert payload["visual_type"] == "chart"
    assert payload["chart_type"] == "bar"
    assert payload["title"] == "Revenue by category"
    assert payload["x_field"] == "category"
    assert payload["y_field"] == "revenue"
    assert payload["value_format"] == "currency"
    assert payload["data"] == [
        {"category": "Hardware", "revenue": 1200.0},
        {"category": "Software", "revenue": 800.0},
        {"category": "Services", "revenue": 450.0},
    ]


def test_field_resolution_case_insensitive() -> None:
    payload = build_visual_payload(
        _bar_hint(x_field="Category", y_field="REVENUE"), _BAR_ROWS, _BAR_COLUMNS
    )
    assert payload is not None
    assert payload["x_field"] == "category"
    assert payload["y_field"] == "revenue"


def test_field_resolution_table_qualified_leaf() -> None:
    # The model sometimes emits the DAX-side name; the leaf must resolve.
    payload = build_visual_payload(
        _bar_hint(x_field="products[category]", y_field="sales[revenue]"),
        _BAR_ROWS,
        _BAR_COLUMNS,
    )
    assert payload is not None
    assert payload["x_field"] == "category"
    assert payload["y_field"] == "revenue"


# ── Percent normalization ───────────────────────────────────────────────


def test_percent_normalization_only_in_zero_one_interval() -> None:
    rows = [
        {"channel": "Direct", "rate": 0.42},  # ratio → ×100
        {"channel": "Referral", "rate": 1.0},  # boundary is included → 100
        {"channel": "Paid", "rate": 37.5},  # already a percentage → untouched
    ]
    payload = build_visual_payload(
        _bar_hint(x_field="channel", y_field="rate", value_format="percent"),
        rows,
        ["channel", "rate"],
    )
    assert payload is not None
    values = [point["rate"] for point in payload["data"]]
    assert values == [42.0, 100.0, 37.5]


def test_no_percent_normalization_for_other_formats() -> None:
    rows = [{"category": "A", "revenue": 0.42}]
    payload = build_visual_payload(_bar_hint(value_format="currency"), rows, _BAR_COLUMNS)
    assert payload is not None
    assert payload["data"][0]["revenue"] == 0.42


# ── Grouped bar ─────────────────────────────────────────────────────────


def test_grouped_bar_fills_missing_combos_with_zero() -> None:
    rows = [
        {"month": "2026-01", "region": "North", "revenue": 10.0},
        {"month": "2026-01", "region": "South", "revenue": 5.0},
        {"month": "2026-02", "region": "North", "revenue": 7.0},
        # 2026-02 / South is missing on purpose.
    ]
    hint = {
        "chart_type": "grouped_bar",
        "x_field": "month",
        "y_field": "revenue",
        "title": "Revenue by month and region",
    }
    payload = build_visual_payload(hint, rows, ["month", "region", "revenue"])
    assert payload is not None
    assert payload["chart_type"] == "grouped_bar"
    assert payload["series"] == ["North", "South"]
    # Missing combo filled with 0.0 so the frontend's bars stay aligned.
    assert payload["data"] == [
        {"month": "2026-01", "North": 10.0, "South": 5.0},
        {"month": "2026-02", "North": 7.0, "South": 0.0},
    ]


def test_grouped_bar_requires_two_categoricals() -> None:
    hint = {"chart_type": "grouped_bar", "x_field": "category", "y_field": "revenue"}
    assert build_visual_payload(hint, _BAR_ROWS, _BAR_COLUMNS) is None


# ── Detail table ────────────────────────────────────────────────────────


def test_table_caps_50_rows_8_cols_but_reports_real_row_count() -> None:
    columns = [f"col_{i}" for i in range(10)]
    rows = [{col: f"v{i}" for col in columns} for i in range(60)]
    payload = build_visual_payload({"chart_type": "table", "title": "Detail"}, rows, columns)
    assert payload is not None
    assert payload["visual_type"] == "detail_table"
    assert len(payload["columns"]) == 8
    assert [spec["key"] for spec in payload["columns"]] == columns[:8]
    assert len(payload["data"]) == 50
    assert set(payload["data"][0].keys()) == set(columns[:8])
    # row_count is the REAL total, so the frontend can say "50 of 60".
    assert payload["row_count"] == 60


def test_table_value_format_applies_to_numeric_columns_only() -> None:
    rows = [{"product": "Widget", "revenue": 99.5}]
    payload = build_visual_payload(
        {"chart_type": "table", "value_format": "currency"}, rows, ["product", "revenue"]
    )
    assert payload is not None
    specs = {spec["key"]: spec for spec in payload["columns"]}
    assert "value_format" not in specs["product"]
    assert specs["revenue"]["value_format"] == "currency"


# ── KPI cards ───────────────────────────────────────────────────────────


def test_kpi_single_row_multi_column() -> None:
    rows = [{"Total Revenue": 125000.0, "Total Orders": 830}]
    payload = build_visual_payload(
        {"chart_type": "kpi", "title": "Monthly KPIs"},
        rows,
        ["Total Revenue", "Total Orders"],
    )
    assert payload is not None
    assert payload["visual_type"] == "kpi_cards"
    assert payload["cards"] == [
        {"label": "Total Revenue", "value": 125000.0},
        {"label": "Total Orders", "value": 830.0},
    ]


def test_kpi_few_rows_uses_categorical_as_label() -> None:
    rows = [
        {"region": "North", "revenue": 10.0},
        {"region": "South", "revenue": 20.0},
    ]
    payload = build_visual_payload({"chart_type": "kpi"}, rows, ["region", "revenue"])
    assert payload is not None
    assert payload["cards"] == [
        {"label": "North", "value": 10.0},
        {"label": "South", "value": 20.0},
    ]


def test_kpi_with_no_numeric_data_returns_none() -> None:
    rows = [{"a": "x", "b": "y"}]
    assert build_visual_payload({"chart_type": "kpi"}, rows, ["a", "b"]) is None


# ── Null hygiene and the bar→grouped upgrade ────────────────────────────


def test_bar_hint_over_two_categorical_dims_upgrades_to_grouped_bar() -> None:
    # The model often hints "bar" for a two-dimensional breakdown. A plain
    # bar would silently drop the second dimension and duplicate x labels
    # (one bar per underlying row) — upgrade to grouped_bar instead.
    rows = [
        {"region": "North", "channel": "Web", "revenue": 10.0},
        {"region": "North", "channel": "Store", "revenue": 5.0},
        {"region": "South", "channel": "Web", "revenue": 7.0},
    ]
    payload = build_visual_payload(
        {"chart_type": "bar", "x_field": "region", "y_field": "revenue"},
        rows,
        ["region", "channel", "revenue"],
    )
    assert payload is not None
    assert payload["chart_type"] == "grouped_bar"
    assert payload["series"] == ["Web", "Store"]
    assert payload["data"][0] == {"region": "North", "Web": 10.0, "Store": 5.0}


def test_bar_chart_drops_rows_with_null_x() -> None:
    rows = [
        {"region": None, "revenue": 3.0},
        {"region": "South", "revenue": 7.0},
    ]
    payload = build_visual_payload(
        {"chart_type": "bar", "x_field": "region", "y_field": "revenue"},
        rows,
        ["region", "revenue"],
    )
    assert payload is not None
    assert payload["data"] == [{"region": "South", "revenue": 7.0}]


def test_grouped_bar_drops_null_series_and_null_x_rows() -> None:
    # A null series must not become a literal "None" legend entry, and a
    # null x must not bucket as its own category.
    rows = [
        {"region": "North", "channel": None, "revenue": 1.0},
        {"region": None, "channel": "Web", "revenue": 2.0},
        {"region": "North", "channel": "Web", "revenue": 10.0},
        {"region": "South", "channel": "Web", "revenue": 7.0},
    ]
    payload = build_visual_payload(
        {"chart_type": "grouped_bar", "x_field": "region", "y_field": "revenue"},
        rows,
        ["region", "channel", "revenue"],
    )
    assert payload is not None
    assert payload["series"] == ["Web"]
    assert payload["data"] == [
        {"region": "North", "Web": 10.0},
        {"region": "South", "Web": 7.0},
    ]


def test_kpi_drops_cards_with_null_labels() -> None:
    rows = [
        {"region": None, "revenue": 3.0},
        {"region": "South", "revenue": 7.0},
    ]
    payload = build_visual_payload({"chart_type": "kpi"}, rows, ["region", "revenue"])
    assert payload is not None
    assert payload["cards"] == [{"label": "South", "value": 7.0}]
