# CLAUDE.md — AI Data Analyst API (starter template)

Persistent context for the AI agent working on this repository. Read first.

## What this is

A single-service FastAPI backend for a conversational analytics product:
natural-language questions → LLM tool-calling loop → validated DAX →
Power BI semantic model → analytical answer + typed visual payload.
Auth (WorkOS-verified, app-owned opaque sessions) lives in the SAME app.
This starter was derived from a production implementation; its constraints
are measured, not guessed.

This is a **template the user characterizes**, not a finished product. As
shipped, every business detail belongs to a fictional org (**ACME
Analytics**). Until the user says otherwise, assume the standing job is
adaptation: author `app/context_pack/BUSINESS.md`, `SCHEMA.md` and
`EXAMPLES.md` for THEIR semantic model (gate:
`scripts/validate_context_pack.py`), fill `.env` from `.env.example`
(never commit it), and replace `ACME` references with their org. The
ordered build path with validation gates lives in the Execution Knowledge
portal (https://ai-data-analyst-three-eta.vercel.app) — follow it when the
user is working through missions.

## Architecture map

```
app/main.py          endpoints: /health, /chat, /chat/reset, /conversations*
                     (auth router adds /auth/*, /admin/users,
                     /admin/sessions/cleanup; follow-up suggestions travel
                     inside the /chat envelope, not a separate endpoint)
app/auth/            sessions.py (opaque tokens, get_current_user)
                     router.py (WorkOS headless login, forgot/reset, admin)
app/ai/              prompts.py (2-block cached system prompt + tool schema)
                     orchestrator.py (stateless loop, ≤6 iterations)
                     dax_guard.py (instructive pre-flight validation)
                     visual_builder.py (typed visual payloads)
app/context_pack/    BUSINESS.md + SCHEMA.md + EXAMPLES.md — the model's brain
app/adapters/powerbi OAuth2 client-credentials + executeQueries + error map
app/db/              pool.py (wait-then-503) + store.py (BIGSERIAL ordering)
migrations/          idempotent SQL baseline (apply in order)
scripts/             live gates: validate_powerbi_auth, validate_dax_generation,
                     validate_context_pack, export_model_schema, smoke_chat
tests/               mocked gate suite — run without any live credentials
```

## Run / test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill values; missing required vars fail at boot BY DESIGN
.venv/bin/python -m pytest tests/ -q
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Hard rules (do not violate, do not "fix")

- **Never commit `.env`** or write secret values into any tracked file.
- **Never weaken the DAX guard** or the tenant/ownership checks.
- **Never add URL or credential fallbacks** — missing config fails loudly.
- **Never put per-request values (dates!) into the cached prompt block** —
  the date travels as a user message precisely to preserve the cache.
- **Blocking I/O endpoints stay sync `def`** (threadpool); an `async def`
  with psycopg2/SDK calls stalls the event loop.
- **Timeout cascade is ordered**: LLM client 85s < frontend 90s < edge
  ~100s. Do not raise the inner ones past the outer ones.
- **No number in an answer that didn't come from returned rows** — the
  anti-fabrication rule is part of the prompt; keep it.
- Message ordering is BIGSERIAL by design (the source's MAX+1 had a race);
  don't reintroduce read-then-write ordering.
- Anything in a mission's "Human Actions" (console clicks, payments,
  admin consent) belongs to the human — ask, don't attempt or invent.

## Known-debt ledger (deliberate, documented)

- Per-request HTTP connections to Power BI (no shared httpx client) — fine
  at this scale; revisit under sustained load.
- History re-sent at input price each turn (no second cache breakpoint) —
  measure with usage metering before optimizing.
- No streaming — see ADR-008; the frontend's UX compensates.
