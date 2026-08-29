"""
Endpoint-level auth tests for the FastAPI app (app/main.py + app/auth/router.py).

Runs the real app through TestClient with the DB-touching layers monkeypatched
(validate_token, the ConversationStore instance, the WorkOS client factory).
What is pinned here:

- EVERY protected endpoint returns 401 without a token — the paid surface has
  no anonymous corner.
- Expired/revoked → 401; valid-but-suspended → 403 (different frontend flows).
- Owner checks: /chat says 403 to a foreign session; the conversation routes
  say 404 — never confirming that someone else's session id exists.
- Anti-oracle auth endpoints: login collapses every WorkOS failure into one
  stable 401; forgot-password ALWAYS answers 200.
- reset-password enforces the local length policy BEFORE spending a WorkOS
  call (the fake factory raises if touched).
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.auth.router as auth_router
import app.auth.sessions as sessions
import app.main as main

client = TestClient(main.app, raise_server_exceptions=False)

_ACTIVE_USER: dict[str, Any] = {
    "id": 7,
    "email": "analyst@acme.example",
    "name": "Ana Lyst",
    "role": "member",
    "status": "active",
}

_AUTH = {"Authorization": "Bearer fake-session-token"}

# (method, path, json_body) for every endpoint that must demand a token.
_PROTECTED_ENDPOINTS = [
    ("GET", "/auth/me", None),
    ("POST", "/chat", {"session_id": "s1", "message": "hello"}),
    ("GET", "/conversations", None),
    ("GET", "/conversations/s1/messages", None),
    ("PATCH", "/conversations/s1/title", {"title": "Renamed"}),
    ("DELETE", "/conversations/s1", None),
    ("POST", "/chat/reset", {"session_id": "s1"}),
]


@pytest.fixture
def as_active_user(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(sessions, "validate_token", lambda token: dict(_ACTIVE_USER))
    return _ACTIVE_USER


class FakeWorkOSError(Exception):
    pass


class FakeRateLimited(FakeWorkOSError):
    pass


def _install_fake_workos(monkeypatch: pytest.MonkeyPatch, user_management: Any) -> None:
    class FakeWorkOSClient:
        pass

    fake = FakeWorkOSClient()
    fake.user_management = user_management
    monkeypatch.setattr(
        auth_router, "_get_workos", lambda: (fake, FakeWorkOSError, FakeRateLimited)
    )


# ── Public surface ──────────────────────────────────────────────────────


def test_health_requires_no_auth() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_admin_endpoint_is_disabled_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    response = client.post(
        "/admin/users", json={"email": "new@acme.example", "name": "New User"}
    )
    # Unset key = admin surface DISABLED, never open.
    assert response.status_code == 503


# ── 401 without a token, across the whole protected surface ─────────────


@pytest.mark.parametrize("method,path,body", _PROTECTED_ENDPOINTS)
def test_every_protected_endpoint_401_without_token(
    method: str, path: str, body: Any
) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", _PROTECTED_ENDPOINTS)
def test_expired_or_revoked_token_401(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
) -> None:
    # validate_token's query filters expired AND revoked rows, so both look
    # identical here: no user comes back.
    monkeypatch.setattr(sessions, "validate_token", lambda token: None)
    response = client.request(method, path, json=body, headers=_AUTH)
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_or_expired_token"


def test_inactive_user_403(monkeypatch: pytest.MonkeyPatch) -> None:
    suspended = dict(_ACTIVE_USER, status="suspended")
    monkeypatch.setattr(sessions, "validate_token", lambda token: suspended)
    response = client.get("/auth/me", headers=_AUTH)
    assert response.status_code == 403
    assert response.json()["detail"] == "account_inactive"


def test_me_returns_profile_for_active_user(as_active_user: dict[str, Any]) -> None:
    response = client.get("/auth/me", headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "email": "analyst@acme.example",
        "name": "Ana Lyst",
        "role": "member",
    }


# ── Owner checks ────────────────────────────────────────────────────────


def test_chat_403_for_foreign_session(
    monkeypatch: pytest.MonkeyPatch, as_active_user: dict[str, Any]
) -> None:
    monkeypatch.setattr(main._store, "check_rate_limit", lambda *a, **k: True)
    monkeypatch.setattr(main._store, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(main._store, "check_session_owner", lambda *a, **k: False)
    response = client.post(
        "/chat", json={"session_id": "someone-elses-session", "message": "hello"}, headers=_AUTH
    )
    assert response.status_code == 403


def test_conversation_routes_404_for_foreign_session(
    monkeypatch: pytest.MonkeyPatch, as_active_user: dict[str, Any]
) -> None:
    # Deliberately 404, not 403: don't confirm a foreign session id exists.
    monkeypatch.setattr(main._store, "check_session_owner", lambda *a, **k: False)
    assert client.get("/conversations/foreign/messages", headers=_AUTH).status_code == 404
    assert (
        client.patch(
            "/conversations/foreign/title", json={"title": "x"}, headers=_AUTH
        ).status_code
        == 404
    )
    assert client.delete("/conversations/foreign", headers=_AUTH).status_code == 404
    assert (
        client.post("/chat/reset", json={"session_id": "foreign"}, headers=_AUTH).status_code
        == 404
    )


def test_chat_rate_limit_fires_before_any_spend(
    monkeypatch: pytest.MonkeyPatch, as_active_user: dict[str, Any]
) -> None:
    monkeypatch.setattr(main._store, "check_rate_limit", lambda *a, **k: False)

    def must_not_be_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("rate limit must short-circuit before session/DB work")

    monkeypatch.setattr(main._store, "ensure_session", must_not_be_called)
    response = client.post("/chat", json={"session_id": "s1", "message": "hi"}, headers=_AUTH)
    assert response.status_code == 429


# ── Anti-oracle auth endpoints ──────────────────────────────────────────


def test_login_workos_error_maps_to_stable_401(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingUserManagement:
        def authenticate_with_password(self, email: str, password: str) -> Any:
            # Wrong password, unknown email, disabled user — the caller must
            # not be able to tell which from the response.
            raise FakeWorkOSError("does not matter which failure")

    _install_fake_workos(monkeypatch, FailingUserManagement())
    response = client.post(
        "/auth/login", json={"email": "anyone@acme.example", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_credentials"


def test_forgot_password_always_200(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingUserManagement:
        def reset_password(self, email: str) -> None:
            raise FakeWorkOSError("user not found")  # must be swallowed

    _install_fake_workos(monkeypatch, FailingUserManagement())
    response = client.post("/auth/forgot-password", json={"email": "unknown@acme.example"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    class QuietUserManagement:
        def reset_password(self, email: str) -> None:
            return None

    _install_fake_workos(monkeypatch, QuietUserManagement())
    response = client.post("/auth/forgot-password", json={"email": "known@acme.example"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}  # byte-identical either way


def test_reset_password_local_length_check_never_touches_workos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_factory() -> Any:
        raise AssertionError("WorkOS must not be called for a locally-invalid password")

    monkeypatch.setattr(auth_router, "_get_workos", exploding_factory)
    response = client.post(
        "/auth/reset-password", json={"token": "reset-token", "new_password": "short"}
    )
    assert response.status_code == 400
    assert "password_too_short" in response.json()["detail"]


def test_reset_password_invalid_token_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    class RejectingUserManagement:
        def confirm_password_reset(self, token: str, new_password: str) -> None:
            raise FakeWorkOSError("token expired")

    _install_fake_workos(monkeypatch, RejectingUserManagement())
    response = client.post(
        "/auth/reset-password",
        json={"token": "stale-token", "new_password": "long-enough-password"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_or_expired_token"


def test_logout_is_idempotent_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Logout revokes when a token is presented; with none it still answers ok.
    response = client.post("/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── Login outcome matrix (the 403 family and the happy path) ────────────


class _WorkOSUser:
    def __init__(self, email: str, verified: bool = True) -> None:
        self.email = email
        self.email_verified = verified


class _AuthOK:
    def __init__(self, email: str, verified: bool = True) -> None:
        self.user = _WorkOSUser(email, verified)


def test_login_unverified_email_is_403_before_membership_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UM:
        def authenticate_with_password(self, email: str, password: str) -> Any:
            return _AuthOK("ana@acme.example", verified=False)

    _install_fake_workos(monkeypatch, UM())

    def must_not_be_called(email: str) -> Any:
        raise AssertionError("membership lookup must not run for unverified emails")

    monkeypatch.setattr(auth_router, "_find_user_by_email", must_not_be_called)
    response = client.post(
        "/auth/login", json={"email": "ana@acme.example", "password": "correct"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "email_not_verified"


def test_login_correct_password_without_provisioning_is_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two-sided provisioning: a valid WorkOS credential alone must not grant
    # access — the user must also exist locally.
    class UM:
        def authenticate_with_password(self, email: str, password: str) -> Any:
            return _AuthOK("stranger@acme.example")

    _install_fake_workos(monkeypatch, UM())
    monkeypatch.setattr(auth_router, "_find_user_by_email", lambda email: None)
    response = client.post(
        "/auth/login", json={"email": "stranger@acme.example", "password": "correct"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "user_not_provisioned"


def test_login_inactive_account_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    class UM:
        def authenticate_with_password(self, email: str, password: str) -> Any:
            return _AuthOK("ana@acme.example")

    _install_fake_workos(monkeypatch, UM())
    suspended = dict(_ACTIVE_USER, status="suspended")
    monkeypatch.setattr(auth_router, "_find_user_by_email", lambda email: suspended)
    response = client.post(
        "/auth/login", json={"email": "ana@acme.example", "password": "correct"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "account_inactive"


def test_login_rate_limited_maps_to_429(monkeypatch: pytest.MonkeyPatch) -> None:
    class UM:
        def authenticate_with_password(self, email: str, password: str) -> Any:
            raise FakeRateLimited("slow down")

    _install_fake_workos(monkeypatch, UM())
    response = client.post(
        "/auth/login", json={"email": "ana@acme.example", "password": "x"}
    )
    assert response.status_code == 429


def test_login_happy_path_mints_opaque_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class UM:
        def authenticate_with_password(self, email: str, password: str) -> Any:
            return _AuthOK("analyst@acme.example")

    _install_fake_workos(monkeypatch, UM())
    monkeypatch.setattr(auth_router, "_find_user_by_email", lambda email: dict(_ACTIVE_USER))

    from datetime import UTC, datetime, timedelta

    expires = datetime.now(UTC) + timedelta(hours=24)
    monkeypatch.setattr(
        auth_router, "create_session", lambda user_id: ("fake-opaque-token", expires)
    )
    response = client.post(
        "/auth/login", json={"email": "analyst@acme.example", "password": "correct"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token"] == "fake-opaque-token"
    assert body["expires_at"] == expires.isoformat()
    assert body["user"] == {"email": "analyst@acme.example", "name": "Ana Lyst"}
