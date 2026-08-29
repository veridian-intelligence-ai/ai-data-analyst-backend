# AI Data Analyst — API starter template

A template for building **your own AI Data Analyst backend** — not a finished
product. One FastAPI service that authenticates users (WorkOS + opaque DB
sessions), runs an LLM ↔ Power BI self-correction loop per question
(natural language → validated DAX → rows → answer), and persists
conversations with typed visual payloads.

It is a generalized, sanitized reference implementation derived from a
production system. Every business detail in it belongs to a **fictional
example org, ACME Analytics** — replacing that with your own business is the
whole point, and the docstrings carry the production lessons behind each
decision so you know what to keep.

## Get your own copy

Press **Use this template → Create a new repository** at the top of this
repo's GitHub page (do **not** fork — a fork drags along the upstream link;
the template button gives you a clean, single-commit repo of your own).
Then clone your new repo and work there.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill every __SET_ME__
psql "$DATABASE_URL" -f migrations/001_baseline.sql   # see migrations/README.md

uvicorn app.main:app --reload
```

Then:

1. Provision yourself: `POST /admin/users` with `X-Admin-Key` (and create
   the matching user in WorkOS — provisioning is two-sided by design; see
   `app/auth/router.py`).
2. `POST /auth/login` → token.
3. `POST /chat` with `Authorization: Bearer <token>` and
   `{"session_id": "s-1", "message": "..."}`.

`GET /health` is the only public endpoint. Deployment: the `Dockerfile`
runs `uvicorn app.main:app` and respects the platform's `PORT`.

Tests run without any live credentials:

```bash
pip install pytest
python -m pytest tests/ -q
```

## Make it yours (what to tell your AI agent)

The template boots against ACME Analytics' fictional semantic model. To
point it at **your** data, work through these concrete steps — each names
the actual file:

1. **Author your context pack** — the model's brain. Fill in, with your own
   business definitions:
   - `app/context_pack/BUSINESS.md` — your metrics, business rules, and
     vocabulary (what "revenue" means *here*, mandatory default filters, …)
   - `app/context_pack/SCHEMA.md` — your semantic model's tables, columns,
     and relationships (`scripts/export_model_schema.py` generates a draft
     from a live model)
   - `app/context_pack/EXAMPLES.md` — validated question → DAX pairs from
     your model
   - `app/context_pack/README.md` explains the three layers. The service
     **refuses to boot without the pack files**, and
     `scripts/validate_context_pack.py` proves the pack is structurally
     sound and internally consistent before it becomes the cached prompt.
2. **Configure your `.env`** from `.env.example`: Anthropic API key, your
   Power BI tenant/client/secret, workspace + dataset IDs, database URL,
   WorkOS credentials. No fallbacks exist — missing config fails loudly at
   boot, by design.
3. **Replace the ACME references** — search the repo for `ACME` and swap in
   your own org's name and examples (docs, `.env.example` comments, tests
   that assert on example vocabulary).
4. **Validate the live seams** one at a time: `scripts/validate_powerbi_auth.py`
   (service principal can reach your workspace), then
   `scripts/validate_dax_generation.py` (questions → DAX against your model),
   then `scripts/smoke_chat.py` (full round-trip).

## Layout

- `app/context_pack/` — the three-layer model context you author for your
  own semantic model (business, schema, examples).
- `app/ai/` — prompt assembly, orchestrator loop, DAX guard, visual builder.
- `app/adapters/powerbi/` — REST adapter (service-principal auth, no ODBC).
- `app/auth/` + `app/db/` — WorkOS headless login, opaque sessions,
  conversation store, usage metering, rate limiting.

Architecture decision on record: one **single-tenant** instance per buyer,
one service. No tenants table, no microservices.

## Where the real instructions live

This README gets you a running copy. The **ordered build path** — which seam
to open in which order, the validation gates that prove each step, and the
failure-recovery playbooks — lives in the Execution Knowledge portal's
AI Data Analyst project: <https://ai-data-analyst-three-eta.vercel.app>.
The companion frontend template is
[ai-data-analyst-frontend](https://github.com/veridian-intelligence-ai/ai-data-analyst-frontend).
