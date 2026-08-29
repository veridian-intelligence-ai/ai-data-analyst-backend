"""
Opaque, DB-backed session tokens — deliberately NOT JWT.

Production-validated decision. For a single-tenant B2B instance with one
Postgres already in the stack, opaque tokens win on every axis that matters:

- INSTANT REVOCATION: logout / suspension takes effect on the next request,
  because every request checks the DB row. A JWT stays valid until expiry
  unless you build a denylist — which is a DB lookup anyway, so the JWT's
  statelessness bought nothing.
- NO KEY MANAGEMENT: no signing keys to rotate, no algorithm confusion
  (alg=none), no library CVE surface. The token is 48 random bytes; the only
  secret is the database.
- ONE SOURCE OF TRUTH: `auth.sessions` is queryable — "who is logged in
  right now" is a SELECT, not a guess.

The cost is one indexed primary-key lookup per request. At this product's
scale that is noise; JWTs earn their complexity when validators cannot reach
the auth database, which is not this architecture.
"""
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db.pool import get_connection, release_connection

_TOKEN_BYTES = 48
_DEFAULT_EXPIRE_HOURS = 24

_bearer_scheme = HTTPBearer(auto_error=False)


def _expire_hours() -> int:
    return int(os.getenv("AUTH_TOKEN_EXPIRE_HOURS", str(_DEFAULT_EXPIRE_HOURS)))


def create_session(user_id: int) -> tuple[str, datetime]:
    """Mint an opaque token for the user. Returns (token, expires_at)."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = datetime.now(UTC) + timedelta(hours=_expire_hours())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth.sessions (token, user_id, expires_at)
                VALUES (%s, %s, %s)
                """,
                (token, user_id, expires_at),
            )
        conn.commit()
    finally:
        release_connection(conn)
    print(f"[auth] session created for user_id={user_id}")
    return token, expires_at


def validate_token(token: str) -> dict[str, Any] | None:
    """
    Resolve a token to its user row, or None.

    One query checks everything: token exists, not expired, not revoked —
    and joins auth.users so the caller also gets the account's status.
    Returns {id, email, name, role, status} or None.
    """
    if not token:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.email, u.name, u.role, u.status
                FROM auth.sessions s
                JOIN auth.users u ON u.id = s.user_id
                WHERE s.token = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                """,
                (token,),
            )
            row = cur.fetchone()
    finally:
        release_connection(conn)
    if row is None:
        return None
    return {"id": row[0], "email": row[1], "name": row[2], "role": row[3], "status": row[4]}


def revoke_token(token: str) -> None:
    """Revoke a session token (idempotent). Effective on the next request."""
    if not token:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth.sessions
                SET revoked_at = now()
                WHERE token = %s AND revoked_at IS NULL
                """,
                (token,),
            )
        conn.commit()
    finally:
        release_connection(conn)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict[str, Any]:
    """
    FastAPI dependency: Bearer token → user dict.

    401 for a missing/invalid/expired token (the client should re-login);
    403 for a valid token on a suspended account (re-login won't help —
    the distinction matters to the frontend's error handling).
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="not_authenticated")
    user = validate_token(credentials.credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid_or_expired_token")
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="account_inactive")
    return user
