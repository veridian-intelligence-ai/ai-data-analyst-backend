"""
Auth router — headless WorkOS password auth + local provisioning.

Architecture (single-tenant instance, ADR: one FastAPI service):

- WorkOS owns CREDENTIALS: password verification, password reset emails,
  email verification. We never see or store a password.
- The local `auth.users` table owns MEMBERSHIP: only emails provisioned by
  an admin may log in, regardless of what exists on the WorkOS side.

TWO-SIDED PROVISIONING (the story behind POST /admin/users): a user can log
in only when they exist in BOTH places — created in WorkOS (dashboard or
API, where they set a password) AND created here via POST /admin/users.
This is deliberate, NOT an oversight: auto-provisioning on first login
("they authenticated, they must be fine") silently turns your identity
provider's user pool into your access-control list, and anyone who ever gets
added to the WorkOS environment — a test account, another app's user —
gains access to the analyst and its data. The provisioning mission walks
through wiring both sides.

Anti-oracle rules baked into the endpoints:
- login: EVERY WorkOS failure maps to the same 401 `invalid_credentials` —
  the response never reveals whether the email exists.
- forgot-password: ALWAYS 200 with a neutral body, even for unknown emails
  (anti-enumeration). The 403 for unprovisioned users happens only AFTER a
  correct password, so it leaks nothing to guessers.

The WorkOS SDK is imported lazily: a missing package or missing keys yields
a clean 503 `workos_not_configured` on the auth endpoints while the rest of
the service (health, and any future non-WorkOS auth) keeps running. An
optional dependency must never be a boot crash.
"""
from __future__ import annotations

import os
import secrets
import threading
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.auth.sessions import create_session, get_current_user, revoke_token
from app.db.pool import get_connection, release_connection

router = APIRouter()

_MIN_PASSWORD_CHARS = 10

_bearer_scheme = HTTPBearer(auto_error=False)

# ── Lazy WorkOS client ──────────────────────────────────────────────────

_workos_client: Any | None = None
_workos_lock = threading.Lock()


def _get_workos():
    """
    Return (client, WorkOSError, RateLimitExceededError), building the client
    on first use. Raises HTTPException(503) when the SDK or config is absent.
    """
    global _workos_client
    try:
        from workos import RateLimitExceededError, WorkOSClient, WorkOSError
    except ImportError as exc:
        print(f"[auth.router] workos SDK not installed: {exc}")
        raise HTTPException(status_code=503, detail="workos_not_configured") from exc

    api_key = os.getenv("WORKOS_API_KEY", "").strip()
    client_id = os.getenv("WORKOS_CLIENT_ID", "").strip()
    if not api_key or not client_id:
        raise HTTPException(status_code=503, detail="workos_not_configured")

    if _workos_client is None:
        with _workos_lock:
            if _workos_client is None:
                _workos_client = WorkOSClient(api_key=api_key, client_id=client_id)
                print("[auth.router] workos client initialized")

    return _workos_client, WorkOSError, RateLimitExceededError


# ── Request/response models ─────────────────────────────────────────────


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320)
    password: str = Field(..., max_length=200)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., max_length=320)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., max_length=2000)
    new_password: str = Field(..., max_length=200)


class CreateUserRequest(BaseModel):
    email: str = Field(..., max_length=320)
    name: str = Field(..., max_length=200)


# ── Local user lookup ───────────────────────────────────────────────────


def _find_user_by_email(email: str) -> dict[str, Any] | None:
    """Case-insensitive match against auth.users (the membership list)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, email, name, role, status
                FROM auth.users
                WHERE lower(email) = lower(%s)
                """,
                (email.strip(),),
            )
            row = cur.fetchone()
    finally:
        release_connection(conn)
    if row is None:
        return None
    return {"id": row[0], "email": row[1], "name": row[2], "role": row[3], "status": row[4]}


# ── Endpoints ───────────────────────────────────────────────────────────


@router.post("/auth/login")
def login(request: LoginRequest) -> dict[str, Any]:
    """
    Headless password login: WorkOS verifies the credentials, we verify the
    membership, and mint OUR opaque session (see app/auth/sessions.py).
    """
    client, workos_error, rate_limited = _get_workos()
    try:
        auth_response = client.user_management.authenticate_with_password(
            email=request.email.strip(),
            password=request.password,
        )
    except rate_limited as exc:
        raise HTTPException(status_code=429, detail="too_many_requests") from exc
    except workos_error as exc:
        # Stable message for EVERY WorkOS failure — wrong password, unknown
        # email, disabled user — so the endpoint is not an account oracle.
        print(f"[auth.router] login rejected by workos: {type(exc).__name__}")
        raise HTTPException(status_code=401, detail="invalid_credentials") from exc

    workos_user = auth_response.user
    if not getattr(workos_user, "email_verified", False):
        raise HTTPException(status_code=403, detail="email_not_verified")

    # Membership check: correct password is NOT enough (see module docstring).
    user = _find_user_by_email(workos_user.email or "")
    if user is None:
        print("[auth.router] authenticated user is not provisioned locally")
        raise HTTPException(status_code=403, detail="user_not_provisioned")
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="account_inactive")

    token, expires_at = create_session(user["id"])
    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "user": {"email": user["email"], "name": user["name"]},
    }


@router.post("/auth/forgot-password")
def forgot_password(request: ForgotPasswordRequest) -> dict[str, str]:
    """
    Trigger the WorkOS password-reset email. ALWAYS returns 200 with the
    same neutral body — even for unknown emails and even on WorkOS errors —
    so the endpoint cannot be used to enumerate accounts.
    """
    client, workos_error, _ = _get_workos()
    try:
        client.user_management.reset_password(email=request.email.strip())
    except workos_error as exc:
        # Swallow everything (including not-found): the reply must not vary.
        print(f"[auth.router] forgot-password suppressed error: {type(exc).__name__}")
    return {"status": "ok"}


@router.post("/auth/reset-password")
def reset_password(request: ResetPasswordRequest) -> dict[str, str]:
    """Consume the reset token from the email and set the new password."""
    # Local policy check FIRST: fail cheap, before spending a WorkOS call,
    # and keep the error message aligned with the frontend's validation.
    if len(request.new_password) < _MIN_PASSWORD_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"password_too_short_min_{_MIN_PASSWORD_CHARS}_chars",
        )
    client, workos_error, rate_limited = _get_workos()
    try:
        client.user_management.confirm_password_reset(
            token=request.token,
            new_password=request.new_password,
        )
    except rate_limited as exc:
        raise HTTPException(status_code=429, detail="too_many_requests") from exc
    except workos_error as exc:
        # Invalid/expired token, or password rejected by WorkOS policy.
        raise HTTPException(status_code=400, detail="invalid_or_expired_token") from exc
    return {"status": "ok"}


@router.post("/auth/logout")
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict[str, str]:
    """Revoke the presented token. Idempotent — logging out twice is fine."""
    if credentials is not None and credentials.credentials:
        revoke_token(credentials.credentials)
    return {"status": "ok"}


@router.get("/auth/me")
def me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Return the authenticated user's profile (frontend session bootstrap)."""
    return {"email": user["email"], "name": user["name"], "role": user["role"]}


# ── Admin provisioning ──────────────────────────────────────────────────


def _require_admin_key(x_admin_key: str | None = Header(None)) -> None:
    """
    Gate on X-Admin-Key with constant-time comparison (timing oracles on a
    naive == are cheap to exploit against short-circuiting string compares).
    """
    admin_key = os.getenv("ADMIN_API_KEY", "").strip()
    if not admin_key:
        # Unset key = admin surface disabled, not open.
        raise HTTPException(status_code=503, detail="admin_not_configured")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, admin_key):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/admin/users", dependencies=[Depends(_require_admin_key)])
def create_user(request: CreateUserRequest) -> dict[str, Any]:
    """
    Provision the LOCAL side of a user (the membership row).

    Remember the two-sided model (module docstring): after this call the
    user must ALSO exist in WorkOS with a password before they can log in.
    The provisioning mission covers creating the WorkOS user and sending
    their activation email.
    """
    email = request.email.strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="invalid_email")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth.users (email, name)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (email, request.name.strip()),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        release_connection(conn)
    if row is None:
        raise HTTPException(status_code=409, detail="user_already_exists")
    print(f"[auth.router] provisioned local user id={row[0]}")
    return {"id": row[0], "email": email, "name": request.name.strip()}


@router.delete("/admin/sessions/cleanup", dependencies=[Depends(_require_admin_key)])
def cleanup_sessions() -> dict[str, Any]:
    """
    Delete sessions that expired more than 7 days ago.

    auth.sessions grows forever otherwise — every login is a new row and
    nothing deletes them. The 7-day grace keeps recently revoked/expired
    sessions queryable for incident audits ("when did that token stop
    working?") while capping the table. Call it from a cron, or by hand.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM auth.sessions WHERE expires_at < now() - INTERVAL '7 days'"
            )
            deleted = cur.rowcount
        conn.commit()
    finally:
        release_connection(conn)
    print(f"[auth.router] session cleanup deleted={deleted}")
    return {"status": "ok", "deleted": deleted}
