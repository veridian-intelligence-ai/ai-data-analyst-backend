# Semantic Model Context Pack

This directory is **the brain of your AI Data Analyst**. The three files here
are loaded verbatim into the cached system prompt:

| File | Layer | What it carries |
|---|---|---|
| `BUSINESS.md` | Business | Measures catalog, business glossary, DAX conventions, **mandatory rules**, supported questions, grain contract |
| `SCHEMA.md` | Machine | Generated schema: tables, columns, types, join keys, relationships, warnings |
| `EXAMPLES.md` | Few-shot | 5–6 **validated** DAX patterns + anti-patterns |

The files ship filled in for the fictional **ACME Analytics — Sales
Intelligence** model so you can see exactly what good looks like. In Missions
M06–M07 you replace their content with your own model's context. Keep the
section structure — the prompt and its tests depend on it.

Three rules learned in production:

1. **Few-shot examples ARE behavior.** An example that omits a field teaches
   the model to omit it. Validate every example by actually executing it
   (Mission M07's gate — scripts/validate_context_pack.py — does this).
2. **Mandatory rules belong here, not in code.** Default business filters,
   grain limits and naming traps are properties of *your model*; encode them
   once in `BUSINESS.md` and let the prompt enforce them.
3. **A changed pack invalidates the prompt cache — which is correct.** Don't
   put anything here that changes daily (dates travel as messages).
