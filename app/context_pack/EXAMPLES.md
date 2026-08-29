# Validated DAX Examples — `Sales Intelligence`

> **TEMPLATE NOTE (delete when you author yours):** every example here MUST
> have been executed successfully against your real model before it enters
> this file — few-shot examples ARE behavior, and an unvalidated example
> teaches the model your mistakes. Mission M18's validation gate runs each
> one through the adapter. Note how every example carries the mandatory
> filter: repetition is how the rule sticks.

## Example 1 — Single value with explicit column name (ROW pattern)

**Question:** "What was revenue in May 2026?"

```dax
EVALUATE
ROW(
    "Revenue May 2026",
    CALCULATE(
        [Revenue],
        dim_calendar[key_year_month] = 202605,
        dim_customers[is_internal] = 0
    )
)
```

**visual_hint:** none — single values live in the prose, highlighted.
**Why this pattern:** `ROW("Name", expr)` names the column explicitly; a bare
table constructor `{ ... }` would return an anonymous `[Value1]` column that
leaks into visuals (the guard rejects it).

## Example 2 — Top-N ranking (TOPN + ADDCOLUMNS pattern)

**Question:** "Top 5 products by revenue this year"

```dax
EVALUATE
TOPN(
    5,
    ADDCOLUMNS(
        VALUES(dim_products[product_name]),
        "Revenue", CALCULATE(
            [Revenue],
            dim_calendar[is_ytd] = TRUE(),
            dim_customers[is_internal] = 0
        )
    ),
    [Revenue], DESC
)
ORDER BY [Revenue] DESC
```

**visual_hint:** `{"chart_type": "bar", "x_field": "product_name", "y_field": "Revenue", "title": "Top 5 products by revenue (YTD)", "value_format": "currency"}`
**Why this pattern:** `ADDCOLUMNS` over `VALUES` computes the measure per
category; `TOPN` selects the top 5 but does NOT guarantee the returned row
order — the trailing `ORDER BY` does, and a ranking rendered in arbitrary
order reads as a bug. The filter lives inside the `CALCULATE`.

## Example 3 — Compact comparison (UNION of ROWs)

**Question:** "Compare revenue and orders: May vs April 2026"

```dax
EVALUATE
UNION(
    ROW(
        "Period", "April 2026",
        "Revenue", CALCULATE([Revenue], dim_calendar[key_year_month] = 202604, dim_customers[is_internal] = 0),
        "Orders", CALCULATE([Orders], dim_calendar[key_year_month] = 202604, dim_customers[is_internal] = 0)
    ),
    ROW(
        "Period", "May 2026",
        "Revenue", CALCULATE([Revenue], dim_calendar[key_year_month] = 202605, dim_customers[is_internal] = 0),
        "Orders", CALCULATE([Orders], dim_calendar[key_year_month] = 202605, dim_customers[is_internal] = 0)
    )
)
```

**visual_hint:** none — a compact 2-period comparison goes in a markdown
table in the text, with Δ% and Δ absolute computed in the narrative.

## Example 4 — Aggregation with axis + temporal filter (SUMMARIZECOLUMNS)

**Question:** "Revenue by region in the last 12 months"

```dax
EVALUATE
SUMMARIZECOLUMNS(
    dim_regions[region_name],
    FILTER(ALL(dim_calendar), dim_calendar[is_last_12_months] = TRUE()),
    FILTER(ALL(dim_customers[is_internal]), dim_customers[is_internal] = 0),
    "Revenue", [Revenue]
)
ORDER BY [Revenue] DESC
```

**visual_hint:** `{"chart_type": "bar", "x_field": "region_name", "y_field": "Revenue", "title": "Revenue by region (last 12 months)", "value_format": "currency"}`
**Why this pattern:** temporal filters inside `SUMMARIZECOLUMNS` go through
`FILTER(ALL(dim_calendar), ...)` so the flag applies cleanly to the axis scan.
The mandatory filter is COLUMN-scoped — `ALL(dim_customers[is_internal])`,
not `ALL(dim_customers)` — so it clears only that column: table-scoped `ALL`
would wipe every dim_customers filter, including a `segment` axis the same
query groups by (see Example 5).

## Example 5 — Two dimensions + one measure (grouped bar)

**Question:** "Revenue by region and segment this year"

```dax
EVALUATE
SUMMARIZECOLUMNS(
    dim_regions[region_name],
    dim_customers[segment],
    FILTER(ALL(dim_calendar), dim_calendar[is_ytd] = TRUE()),
    FILTER(ALL(dim_customers[is_internal]), dim_customers[is_internal] = 0),
    "Revenue", [Revenue]
)
```

**visual_hint:** `{"chart_type": "grouped_bar", "x_field": "region_name", "y_field": "Revenue", "title": "Revenue by region and segment (YTD)", "value_format": "currency"}`

## Example 6 — Time series (line)

**Question:** "Monthly revenue evolution in 2026"

```dax
EVALUATE
SUMMARIZECOLUMNS(
    dim_calendar[key_year_month],
    FILTER(ALL(dim_calendar), dim_calendar[year] = 2026),
    FILTER(ALL(dim_customers[is_internal]), dim_customers[is_internal] = 0),
    "Revenue", [Revenue]
)
ORDER BY dim_calendar[key_year_month]
```

**visual_hint:** `{"chart_type": "line", "x_field": "key_year_month", "y_field": "Revenue", "title": "Monthly revenue 2026", "value_format": "currency"}`

---

## Anti-patterns (the model must never do these)

❌ **Anonymous table constructor as the top-level EVALUATE:**

```dax
EVALUATE { CALCULATE([Revenue], dim_calendar[key_year_month] = 202605) }
```

Returns `[Value1]` — raw labels leak into visuals. The guard rejects this
before Power BI ever sees it. Correction: Example 1's `ROW` pattern.
(`IN { ... }` filters are fine — the rule only concerns the top-level shape.)

❌ **Filtering on the raw source date:**

```dax
CALCULATE([Revenue], fct_sales[order_date] >= DATE(2026, 5, 1))
```

`order_date` bypasses the calendar; flags and month axes silently disagree.
Correction: filter `dim_calendar` (`key_year_month`, flags, or `KeyDate`).

❌ **Forgetting the mandatory filter:**

```dax
EVALUATE ROW("Revenue", CALCULATE([Revenue], dim_calendar[is_ytd] = TRUE()))
```

Inflated by internal test accounts. Every query carries
`dim_customers[is_internal] = 0` unless the user explicitly lifts it.
