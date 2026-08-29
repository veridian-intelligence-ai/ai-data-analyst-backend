#!/usr/bin/env python3
"""
Context-pack integrity gate (Mission M07).

Proves the pack is structurally sound and internally consistent BEFORE it
becomes the cached system prompt:

1. The three files exist and are non-empty.
2. BUSINESS.md carries the mandatory sections (measures, glossary, mandatory
   rules, grain contract).
3. Every `table[column]`-style identifier used in EXAMPLES.md appears in
   SCHEMA.md — a DAX example referencing a nonexistent column would teach
   the model your typos.
4. If a mandatory filter rule is declared (a line containing "MUST include
   the filter" with a backticked DAX fragment), every example query contains
   that fragment — the source system enforced its rule by repetition across
   all examples, and so should yours.

Exit 0 = pack valid. Non-zero = the printed findings.
"""
from __future__ import annotations

import re
from pathlib import Path

PACK = Path(__file__).parent.parent / "app" / "context_pack"

REQUIRED_BUSINESS_SECTIONS = [
    "## Measures",
    "## Business glossary",
    "## MANDATORY RULES",
    "## Grain contract",
]


def main() -> int:
    findings: list[str] = []

    files = {name: PACK / name for name in ("BUSINESS.md", "SCHEMA.md", "EXAMPLES.md")}
    texts: dict[str, str] = {}
    for name, path in files.items():
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            findings.append(f"{name}: missing or empty")
        else:
            texts[name] = path.read_text(encoding="utf-8")

    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
        return 1

    business = texts["BUSINESS.md"]
    schema = texts["SCHEMA.md"]
    examples = texts["EXAMPLES.md"]

    for section in REQUIRED_BUSINESS_SECTIONS:
        if section.lower() not in business.lower():
            findings.append(f"BUSINESS.md: required section '{section}' not found")

    # Identifiers used in examples must exist in the schema layer.
    # Table names must be contiguous identifiers — allowing spaces here once
    # made `ORDER BY dim_calendar[...]` parse as a table named "ORDER BY
    # dim_calendar", failing CI on a perfectly valid pack.
    identifiers = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\[([a-zA-Z_][a-zA-Z0-9_ ]*)\]", examples))
    for table, column in sorted(identifiers):
        table = table.strip()
        column = column.strip()
        if f"`{table}`" not in schema and f"Table `{table}`" not in schema:
            findings.append(f"EXAMPLES.md references table '{table}' not documented in SCHEMA.md")
        elif f"`{column}`" not in schema and column not in schema:
            findings.append(f"EXAMPLES.md references column '{table}[{column}]' not documented in SCHEMA.md")

    # Measures referenced as [Measure Name] must exist in the schema layer.
    dax_blocks = re.findall(r"```dax\n(.*?)```", examples, re.DOTALL)
    measure_refs = set()
    for block in dax_blocks:
        for match in re.findall(r"(?<![\w\]])\[([A-Za-z][A-Za-z0-9 /%_.-]*)\]", block):
            measure_refs.add(match.strip())
    known_columns = {c.strip() for _, c in identifiers}
    for measure in sorted(measure_refs):
        if measure in known_columns:
            continue
        if measure not in schema:
            findings.append(f"EXAMPLES.md uses measure '[{measure}]' not documented in SCHEMA.md")

    # Mandatory-filter repetition rule.
    rule_match = re.search(r"MUST include the\s+filter\s+`([^`]+)`", business, re.IGNORECASE)
    if rule_match:
        fragment = rule_match.group(1).strip()
        for i, block in enumerate(dax_blocks, 1):
            # Anti-pattern sections deliberately violate the rule; skip blocks
            # that EXAMPLES.md marks as wrong.
            marker = examples.find(block)
            preceding = examples[max(0, marker - 400) : marker]
            if "❌" in preceding or "anti-pattern" in preceding.lower():
                continue
            if fragment.split("=")[0].strip() not in block:
                findings.append(
                    f"EXAMPLES.md dax block #{i} omits the mandatory filter `{fragment}` "
                    "(rule declared in BUSINESS.md — repetition is how it sticks)"
                )

    if len(dax_blocks) < 5:
        findings.append(
            f"EXAMPLES.md has {len(dax_blocks)} DAX blocks; ≥5 validated patterns required "
            "(single value, ranking, comparison, temporal filter, time series)"
        )

    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
        print(f"\n{len(findings)} finding(s). Fix the pack before assembling the prompt.")
        return 1

    print("Context pack integrity: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
