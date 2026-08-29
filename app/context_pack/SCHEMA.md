# Semantic Model `Sales Intelligence` — Schema Layer

> **TEMPLATE NOTE (delete when you author yours):** generate this layer from
> your real model rather than writing it by hand. Two proven routes:
> 1. **Tabular Editor** (free): open your model → Advanced Scripting → export
>    tables/columns/measures/relationships to text, or copy from the TOM
>    tree.
> 2. **DAX Studio / INFO functions**: run `EVALUATE INFO.TABLES()`,
>    `INFO.COLUMNS()`, `INFO.MEASURES()`, `INFO.RELATIONSHIPS()` against the
>    model (needs XMLA read or the executeQueries endpoint) and paste the
>    results here in the table format below.
> Keep column descriptions rich: join keys, value enumerations, and warnings
> are what the model actually uses. Do NOT include workspace/dataset GUIDs
> or data-source connection strings in this file.

Model: `Sales Intelligence` · Compatibility: Tabular · 5 tables · 8 measures · 4 relationships

---

## Table `fct_sales` — fact, one row per order line (daily grain)

| Column | Type | Hidden | Description |
|---|---|---|---|
| `order_id` | string | no | Order identifier. One order has 1..n lines. |
| `order_line_id` | string | yes | Technical PK of the line. |
| `KeyDate` | date | yes | Order date. Join key → `dim_calendar[KeyDate]`. Use this, not `order_date`. |
| `order_date` | date | yes | Raw source date. NOT connected to the calendar — never filter on it. |
| `product_id` | string | yes | Join key → `dim_products[product_id]`. |
| `region_id` | string | yes | Join key → `dim_regions[region_id]`. |
| `customer_id` | string | yes | Join key → `dim_customers[customer_id]`. |
| `qty` | int | no | Units in the line. |
| `net_amount` | decimal | no | Line revenue net of discount, EUR. |
| `discount_amount` | decimal | no | Discount applied to the line, EUR. |

## Table `dim_calendar` — one row per day

| Column | Type | Hidden | Description |
|---|---|---|---|
| `KeyDate` | date | no | PK. |
| `year` | int | no | Calendar year. |
| `month` | int | no | 1–12. |
| `month_name` | string | no | Display axis for months. |
| `key_year_month` | int | no | YYYYMM integer, e.g. 202605. Preferred month filter/axis. |
| `quarter_year` | string | no | e.g. "Q2 2026". |
| `is_ytd` | bool | no | Current year up to yesterday. |
| `is_last_12_months` | bool | no | Rolling 12 complete months. |
| `is_m_1` | bool | no | Last complete month. |

## Table `dim_products`

| Column | Type | Hidden | Description |
|---|---|---|---|
| `product_id` | string | yes | PK. |
| `product_name` | string | no | Display axis. |
| `category` | string | no | One of: Furniture, Lighting, Textiles, Decor. |
| `is_active` | bool | no | Discontinued products have historical sales — do not filter them out of historical questions. |

## Table `dim_regions`

| Column | Type | Hidden | Description |
|---|---|---|---|
| `region_id` | string | yes | PK. |
| `region_name` | string | no | One of: North, South, East, West, Online. |
| `country` | string | no | Country within the region. |

## Table `dim_customers`

| Column | Type | Hidden | Description |
|---|---|---|---|
| `customer_id` | string | yes | PK. |
| `segment` | string | no | `Business` or `Consumer`. |
| `is_internal` | bool | no | ⚠️ MANDATORY RULE: internal/test accounts. Every query filters `is_internal = 0` unless explicitly asked. |
| `first_order_date` | date | no | Date of first purchase. |

---

## Relationships

| From | To | Cardinality | Active |
|---|---|---|---|
| `fct_sales[KeyDate]` | `dim_calendar[KeyDate]` | many-to-one | yes |
| `fct_sales[product_id]` | `dim_products[product_id]` | many-to-one | yes |
| `fct_sales[region_id]` | `dim_regions[region_id]` | many-to-one | yes |
| `fct_sales[customer_id]` | `dim_customers[customer_id]` | many-to-one | yes |

## Measure definitions (full DAX)

```dax
Revenue := SUM(fct_sales[net_amount])
Units Sold := SUM(fct_sales[qty])
Orders := DISTINCTCOUNT(fct_sales[order_id])
Customers := DISTINCTCOUNT(fct_sales[customer_id])
Average Order Value := DIVIDE([Revenue], [Orders])
Units per Order := DIVIDE([Units Sold], [Orders])
Discount Rate := DIVIDE(SUM(fct_sales[discount_amount]), [Revenue] + SUM(fct_sales[discount_amount]))
Row Count Sales := COUNTROWS(fct_sales)
```

Format strings: Revenue/AOV → `#,0.00 €`; Discount Rate → `0.0%`; counts → `#,0`.
