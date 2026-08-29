"""
HTTP smoke test against a RUNNING deployment (local or production).

Usage:

    API_URL=https://api.acme.example TOKEN=<session-token> python3 scripts/smoke_chat.py

    # or against a local run (uvicorn app.main:app):
    API_URL=http://localhost:8000 TOKEN=<session-token> python3 scripts/smoke_chat.py

Two probes, in dependency order:
1. GET /health          — is the service up at all? (no auth)
2. POST /chat           — the full paid path: auth, orchestrator, Power BI.
   Asserts a non-empty answer AND that next_step_suggestions is a list —
   the two fields the frontend cannot render without.

Exit codes: 0 = both probes passed, 1 = a probe failed, 2 = missing config.
Obtain TOKEN via POST /auth/login (it is one of OUR opaque session tokens,
not a WorkOS artifact).
"""
from __future__ import annotations

import os
import sys
import time

import httpx

_TIMEOUT_HEALTH = 15.0
# /chat runs the whole LLM loop — give it the same budget the frontend does.
_TIMEOUT_CHAT = 90.0

_SMOKE_QUESTION = "What was total revenue last month?"


def main() -> int:
    api_url = os.getenv("API_URL", "").strip().rstrip("/")
    token = os.getenv("TOKEN", "").strip()
    if not api_url:
        print("[smoke] API_URL is not set (e.g. http://localhost:8000)")
        return 2
    if not token:
        print("[smoke] TOKEN is not set (get one from POST /auth/login)")
        return 2

    # Probe 1: liveness.
    print(f"[smoke] GET {api_url}/health")
    try:
        response = httpx.get(f"{api_url}/health", timeout=_TIMEOUT_HEALTH)
    except httpx.HTTPError as exc:
        print(f"[smoke] FAILED: /health unreachable: {exc}")
        return 1
    if response.status_code != 200 or response.json().get("status") != "ok":
        print(f"[smoke] FAILED: /health returned HTTP {response.status_code}: {response.text[:200]}")
        return 1
    print("[smoke] /health OK")

    # Probe 2: one authenticated conversation turn.
    session_id = f"smoke-{int(time.time())}"
    print(f"[smoke] POST {api_url}/chat (session {session_id})")
    try:
        response = httpx.post(
            f"{api_url}/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"session_id": session_id, "message": _SMOKE_QUESTION},
            timeout=_TIMEOUT_CHAT,
        )
    except httpx.HTTPError as exc:
        print(f"[smoke] FAILED: /chat unreachable or timed out: {exc}")
        return 1

    if response.status_code == 401:
        print("[smoke] FAILED: 401 — TOKEN is invalid or expired (re-login)")
        return 1
    if response.status_code == 429:
        print("[smoke] FAILED: 429 — rate limited; wait a minute and re-run")
        return 1
    if response.status_code != 200:
        print(f"[smoke] FAILED: /chat returned HTTP {response.status_code}: {response.text[:300]}")
        return 1

    payload = response.json()
    answer = payload.get("answer", "")
    suggestions = payload.get("next_step_suggestions")

    if payload.get("status") != "ok":
        print(f"[smoke] FAILED: /chat status={payload.get('status')} error={payload.get('error')}")
        return 1
    if not isinstance(answer, str) or not answer.strip():
        print("[smoke] FAILED: empty answer")
        return 1
    if not isinstance(suggestions, list):
        print("[smoke] FAILED: next_step_suggestions is not a list")
        return 1

    print(f"[smoke] answer ({len(answer)} chars): {answer[:200]}")
    print(f"[smoke] suggestions: {len(suggestions)} item(s)")
    print("[smoke] PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
