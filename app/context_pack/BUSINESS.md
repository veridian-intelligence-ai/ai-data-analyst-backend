# Semantic Model `Sales Intelligence` — Business Layer

> **TEMPLATE NOTE (delete this block when you author yours):** this file is
> filled in for the fictional ACME Analytics model. Replace every section
> with your own model's content, keeping the section structure. Write it for
> a smart analyst who has never seen your business: every measure, every
> term, every trap.

## Measures (8)

### Sales volumes
| Measure | DAX (simplified) | Source |
|---|---|---|
| Revenue | SUM(fct_sales[net_amount]) | fct_sales |
| Units Sold | SUM(fct_sales[qty]) | fct_sales |
| Orders | DISTINCTCOUNT(fct_sales[order_id]) | fct_sales |
| Customers | DISTINCTCOUNT(fct_sales[customer_id]) | fct_sales |

### Ratios
| Measure | Formula |
|---|---|
| Average Order Value | Revenue / Orders |
| Units per Order | Units Sold / Orders |
| Discount Rate | SUM(fct_sales[discount_amount]) / (Revenue + SUM(fct_sales[discount_amount])) |

### Diagnostics (hidden)
| Measure | Description |
|---|---|
| Row Count Sales | COUNTROWS(fct_sales) — data-load sanity check |

---

## Business glossary — how people talk → what the model calls it

| Term people use | Model artifact | Pitfalls |
|---|---|---|
| "sales", "turnover", "billing" | measure `[Revenue]` | Net of discounts. Gross = Revenue + discount_amount. |
| "ticket", "basket size" | measure `[Average Order Value]` | Per order, not per customer. |
| "clients", "buyers" | measure `[Customers]` | Distinct customers with ≥1 order in the filter context — NOT the customer master count. |
| "top products" | `dim_products[product_name]` axis + `[Revenue]` | Default ranking metric is Revenue unless the user says units. |
| "region" | `dim_regions[region_name]` | Countries roll up into regions; ask which level only if genuinely ambiguous. |
| "B2B / B2C" | `dim_customers[segment]` values `Business` / `Consumer` | Exactly these two values. |

---

## DAX conventions for this model

1. Columns prefixed `_` are technical (surrogate keys, load metadata) — never
   surface them; use measures instead.
2. Use `dim_calendar[KeyDate]` for all date joins. The fact table's raw
   `order_date` column is NOT connected to the calendar — filtering on it
   bypasses the calendar's flags.
3. For display axes prefer `product_name`, `region_name`, `segment` — never
   `product_id`/`region_id` codes.
4. `key_year_month` has the format `YYYYMM` as an integer (e.g. 202605).

---

## MANDATORY RULES — apply to EVERY query, no exceptions

1. **Exclude internal test accounts.** Every DAX query MUST include the
   filter `dim_customers[is_internal] = 0`, EXCEPT when the user explicitly
   asks to analyze internal/test data. This applies to every query shape:
   - `ROW` / single value: `ROW("Name", CALCULATE([Measure], filters, dim_customers[is_internal] = 0))`
   - `SUMMARIZECOLUMNS`: add
     `FILTER(ALL(dim_customers[is_internal]), dim_customers[is_internal] = 0)`
     to the filters — column-scoped `ALL`, never `ALL(dim_customers)`, which
     would also wipe any dim_customers axis the query groups by
   - `TOPN + ADDCOLUMNS`: inside the measure's `CALCULATE`
   - Temporal comparisons: in every `CALCULATE` involved
   Internal accounts are ACME's own test purchases and would inflate every
   metric. NEVER omit this filter.

> **TEMPLATE NOTE:** this is the "default business filter" slot. Almost every
> real model has at least one: test accounts, a partner with separate
> reporting, pre-migration data. Write yours with the exact DAX in both
> FILTER and CALCULATE forms, and state the exception that lifts it.

---

## Supported question types (the acceptance catalog)

| # | Question shape | Measures | Axis | Notes |
|---|---|---|---|---|
| 1 | "What was revenue in {month}?" | Revenue | — | `key_year_month` filter |
| 2 | "Top {N} products by revenue {period}" | Revenue | product_name | TOPN pattern |
| 3 | "Revenue by region this year" | Revenue | region_name | `is_ytd` flag |
| 4 | "Monthly revenue evolution last 12 months" | Revenue | key_year_month axis | `is_last_12_months`, line visual |
| 5 | "{month} vs {month} comparison" | any | — | compact comparison → markdown table, no visual |
| 6 | "AOV by segment" | Average Order Value | segment | |
| 7 | "How many customers bought {product}?" | Customers | — | product filter |
| 8 | "Revenue by region and segment" | Revenue | region_name × segment | grouped_bar |

> **TEMPLATE NOTE:** list 20–40 of these for your model. This catalog doubles
> as your acceptance-test suite: after any prompt change, re-run a sample.

---

## Grain contract

- The model is **daily**: `fct_sales` has one row per order line, dated to
  the order day. Data is loaded nightly — **yesterday is the freshest day**.
- Valid: any question at day grain or coarser (weeks, months, quarters, YTD,
  rolling 12 months).
- NOT answerable: intraday questions ("this morning", "the last hour").
  Standard refusal sentence:
  > "The data is updated nightly — the most recent complete day is
  > {yesterday}. For that day: {value}."
- Calendar flags that work: `is_ytd`, `is_last_12_months`, `is_m_1` (last
  complete month), `key_year_month = YYYYMM`.
- Temporal coverage: 2024-01-01 onward.

> **TEMPLATE NOTE:** the grain contract prevents the single most expensive
> class of silent errors. If your model is weekly or monthly, say so and give
> the exact refusal sentence for finer-grained questions.

---

## Reporting conventions

- Currency: EUR, symbol €, thousands separator by locale of the answer.
- Percentages: one decimal place.
- "Last month" always means the last COMPLETE month.
