"""
AI Data Analyst API — single FastAPI service (auth + chat in one app).

ADR context: the buyer deploys a SINGLE-TENANT instance. One service means
one deploy, one env file, one log stream — the operational simplicity is
worth more to a small team than microservice purity.

SYNC-VS-ASYNC RULE (why every endpoint here is `def`, not `async def`):
this service's hot path is blocking I/O — psycopg2, the Anthropic SDK, httpx
sync calls into Power BI. A blocking call inside `async def` freezes the
whole event loop for every user; a plain `def` endpoint is run by FastAPI in
its threadpool, so a slow Power BI query only occupies one thread. Only make
an endpoint `async def` after every call inside it is truly awaitable.

TIMEOUT CASCADE (each layer must give up BEFORE the layer above it):
    Anthropic SDK 85s < frontend fetch 90s < edge/proxy ~100s
If the inner timeout were the longest, users would see opaque edge 502s
instead of this service's honest error JSON — production learned this the
hard way. The Power BI adapter's own 120s cap applies per DAX call inside
the LLM loop, which the SDK timeout bounds overall.
"""
from __future__ import annotations

import os
import time
import traceback
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.adapters.powerbi import PowerBIAuthenticator, PowerBIClient
from app.ai.orchestrator import DAXConversationOrchestrator
from app.ai.prompts import get_conversation_window
from app.ai.visual_builder import build_visual_payload
from app.auth.router import router as auth_router
from app.auth.sessions import get_current_user
from app.config import get_allowed_origins, require_env
from app.db.pool import PoolTimeout, close_pool
from app.db.store import ConversationStore

# The LLM-side timeout: the bottom of the cascade documented above.
# Env-overridable so operational drills can sabotage it without code edits —
# but the cascade rule stands: keep it BELOW the frontend's 90s.
_ANTHROPIC_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "85"))

# Model per role is an ADR-011 decision: a strong model for DAX generation.
# The env seam exists so bake-offs and drills need no code change.
_ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()

_HISTORY_WINDOW = get_conversation_window()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on everything /chat will need — a half-configured analyst
    # must crash at boot, not 500 on the first paying request.
    require_env("DATABASE_URL")
    require_env("POWERBI_DATASET_ID")
    get_allowed_origins()
    print("[main] startup checks passed")
    yield
    close_pool()


app = FastAPI(title="AI Data Analyst API", lifespan=lifespan)

# Explicit origins + credentials. get_allowed_origins() refuses wildcards.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
)

app.include_router(auth_router)

_store = ConversationStore()

# ── Shared clients (lazy singletons) ────────────────────────────────────
# The orchestrator is stateless and built fresh per request, but the CLIENTS
# under it must be shared: PowerBIAuthenticator caches its OAuth token on the
# instance, so a per-request authenticator pays a full Entra client-credentials
# round-trip on EVERY chat turn — pure added latency the token cache exists to
# remove. Lazy (not import-time) so importing this module never needs
# credentials; the first /chat builds them.

_powerbi_client: PowerBIClient | None = None
_anthropic_client: anthropic.Anthropic | None = None


def _get_powerbi_client() -> PowerBIClient:
    global _powerbi_client
    if _powerbi_client is None:
        _powerbi_client = PowerBIClient(PowerBIAuthenticator())
    return _powerbi_client


def _get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(timeout=_ANTHROPIC_TIMEOUT_SECONDS)
    return _anthropic_client


# ── Error policy: generic to clients, full detail to server logs ────────
# Production lesson: returning str(exc) to clients leaked SQL fragments and
# internal hostnames. The client gets a stable code; the traceback goes to
# the logs, where it belongs.


@app.exception_handler(PoolTimeout)
async def pool_timeout_handler(request: Request, exc: PoolTimeout) -> JSONResponse:
    print(f"[main] pool timeout on {request.url.path}: {exc}")
    return JSONResponse(status_code=503, content={"detail": "service_busy"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    print(f"[main] unhandled error on {request.url.path}: {type(exc).__name__}")
    traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "internal_error"})


# ── Public endpoints ────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — no auth, no dependencies touched."""
    return {"status": "ok", "service": "api"}


# ── Chat ────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=4000)
    debug: bool = False


def _debug_payloads_enabled() -> bool:
    """
    Debug payloads (raw orchestrator output: DAX, rows, iteration logs) are
    gated by an env flag, not just the request field. Production lesson: an
    ungated ?debug=true once exposed internal DAX and full row dumps to any
    authenticated user with a browser devtools tab open.
    """
    return os.getenv("APP_DEBUG_PAYLOADS", "").strip().lower() == "true"


@app.post("/chat")
def chat(request: ChatRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """
    One conversation turn. Sync `def` on purpose — see the module docstring.

    Order of operations matters:
    1. rate limit (before any money is spent)
    2. ensure session + STRICT owner check
    3. fetch history BEFORE appending the new user message — otherwise the
       new question appears both in history and as the current message, and
       the model sees itself asked twice (a real production bug: answers
       started with "As I mentioned...")
    4. append user message, run the orchestrator, persist the envelope
    """
    started = time.monotonic()

    if not _store.check_rate_limit(user["id"], "/chat"):
        raise HTTPException(status_code=429, detail="rate_limited")

    _store.ensure_session(request.session_id, user["id"])
    if not _store.check_session_owner(request.session_id, user["id"]):
        raise HTTPException(status_code=403, detail="forbidden")

    history = _store.get_history(request.session_id, limit=_HISTORY_WINDOW)
    _store.append_message(request.session_id, "user", request.message.strip())

    # Fresh orchestrator per request (stateless by design — see orchestrator)
    # over SHARED clients (token cache lives on the authenticator singleton).
    orchestrator = DAXConversationOrchestrator(
        anthropic_client=_get_anthropic_client(),
        powerbi_client=_get_powerbi_client(),
        dataset_id=require_env("POWERBI_DATASET_ID"),
        model=_ANTHROPIC_MODEL,
    )
    result = orchestrator.run(request.message.strip(), conversation_history=history)

    visual = build_visual_payload(result.visual_hint, result.dax_rows, result.dax_columns)
    response_mode = "visual" if visual else "text"

    _store.append_assistant_message(
        request.session_id,
        answer=result.answer,
        response_mode=response_mode,
        visual=visual,
        next_step_suggestions=result.next_step_suggestions,
    )

    response: dict[str, Any] = {
        "session_id": request.session_id,
        "status": "error" if result.error else "ok",
        "answer": result.answer,
        "response_mode": response_mode,
        "visual": visual,
        "next_step_suggestions": result.next_step_suggestions,
        "error": result.error_type if result.error else None,
    }
    if request.debug and _debug_payloads_enabled():
        response["debug"] = result.to_payload()

    total_ms = int((time.monotonic() - started) * 1000)
    llm_ms = result.total_latency_ms
    powerbi_ms = sum(it.dax_latency_ms or 0 for it in result.iterations)
    # Counts, not just durations: a retry storm (6 iterations burned) at
    # normal total latency is invisible without them, and the per-iteration
    # [llm] lines interleave across concurrent requests.
    n_queries = sum(1 for it in result.iterations if it.dax_latency_ms is not None)
    print(
        f"[timing] total={total_ms}ms | llm={llm_ms}ms "
        f"({len(result.iterations)} iterations) | powerbi={powerbi_ms}ms "
        f"({n_queries} queries) | other={max(total_ms - llm_ms - powerbi_ms, 0)}ms"
    )

    _store.emit_usage_event(
        user_id=user["id"],
        endpoint="/chat",
        input_tokens=result.total_input_tokens,
        output_tokens=result.total_output_tokens,
        cache_read_tokens=result.total_cache_read_tokens,
        cache_write_tokens=result.total_cache_write_tokens,
        latency_ms=total_ms,
        extra={
            "session_id": request.session_id,
            "prompt_version": result.prompt_version,
            "iterations": len(result.iterations),
            "response_mode": response_mode,
            "error_type": result.error_type,
        },
    )
    return response


# ── Conversation management (all Bearer + owner-checked) ────────────────


def _require_owned_session(session_id: str, user: dict[str, Any]) -> None:
    """404, not 403, for sessions that aren't yours: don't confirm existence."""
    if not _store.check_session_owner(session_id, user["id"]):
        raise HTTPException(status_code=404, detail="session_not_found")


@app.get("/conversations")
def list_conversations(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return {"conversations": _store.list_sessions(user["id"])}


@app.get("/conversations/{session_id}/messages")
def get_conversation_messages(
    session_id: str, user: dict[str, Any] = Depends(get_current_user)
) -> dict[str, Any]:
    _require_owned_session(session_id, user)
    return {"session_id": session_id, "messages": _store.get_messages(session_id)}


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@app.patch("/conversations/{session_id}/title")
def rename_conversation(
    session_id: str,
    request: RenameRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    _require_owned_session(session_id, user)
    _store.rename_session(session_id, request.title)
    return {"status": "ok"}


@app.delete("/conversations/{session_id}")
def delete_conversation(
    session_id: str, user: dict[str, Any] = Depends(get_current_user)
) -> dict[str, str]:
    _require_owned_session(session_id, user)
    _store.delete_session(session_id)
    return {"status": "ok"}


class ResetRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=100)


@app.post("/chat/reset")
def reset_chat(
    request: ResetRequest, user: dict[str, Any] = Depends(get_current_user)
) -> dict[str, str]:
    """Clear a session's messages but keep the session (fresh context)."""
    _require_owned_session(request.session_id, user)
    _store.clear_session(request.session_id)
    return {"status": "ok"}
