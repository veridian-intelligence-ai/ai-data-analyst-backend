"""
Unit tests for the opaque-session module (app/auth/sessions.py).

The DB pool is replaced with a fake connection that records SQL and returns
scripted rows — the tests exercise the module's contract, not Postgres. The
distinction pinned here matters to the frontend: 401 means "re-login will
help" (missing/expired/revoked token), 403 means "re-login will NOT help"
(valid token, suspended account).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import app.auth.sessions as sessions


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._conn.fetchone_result


class FakeConnection:
    """Records every execute; returns one scripted fetchone row."""

    def __init__(self, fetchone_result: tuple[Any, ...] | None = None) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.fetchone_result = fetchone_result
        self.committed = False
        self.released = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_conn(monkeypatch: pytest.MonkeyPatch) -> FakeConnection:
    conn = FakeConnection()

    def fake_release(c: Any) -> None:
        conn.released = True

    monkeypatch.setattr(sessions, "get_connection", lambda: conn)
    monkeypatch.setattr(sessions, "release_connection", fake_release)
    return conn


# ── create_session ──────────────────────────────────────────────────────


def test_create_session_mints_random_token_and_commits(fake_conn: FakeConnection) -> None:
    token, expires_at = sessions.create_session(user_id=7)

    assert len(token) >= 48  # 48 random bytes, urlsafe-encoded
    sql, params = fake_conn.executed[0]
    assert "INSERT INTO auth.sessions" in sql
    assert params == (token, 7, expires_at)
    assert fake_conn.committed is True
    assert fake_conn.released is True

    # Default lifetime: 24 hours (within a minute of tolerance).
    delta = expires_at - datetime.now(UTC)
    assert timedelta(hours=23, minutes=59) < delta <= timedelta(hours=24)


def test_create_session_respects_expire_hours_env(
    fake_conn: FakeConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_TOKEN_EXPIRE_HOURS", "1")
    _, expires_at = sessions.create_session(user_id=1)
    delta = expires_at - datetime.now(UTC)
    assert delta <= timedelta(hours=1)


def test_tokens_are_unique(fake_conn: FakeConnection) -> None:
    token_a, _ = sessions.create_session(user_id=1)
    token_b, _ = sessions.create_session(user_id=1)
    assert token_a != token_b


# ── validate_token ──────────────────────────────────────────────────────


def test_validate_token_returns_user_dict(fake_conn: FakeConnection) -> None:
    fake_conn.fetchone_result = (7, "analyst@acme.example", "Ana Lyst", "member", "active")
    user = sessions.validate_token("some-token")
    assert user == {
        "id": 7,
        "email": "analyst@acme.example",
        "name": "Ana Lyst",
        "role": "member",
        "status": "active",
    }
    # The single query enforces existence + not revoked + not expired at once.
    sql, params = fake_conn.executed[0]
    assert "revoked_at IS NULL" in sql
    assert "expires_at > now()" in sql
    assert params == ("some-token",)


def test_validate_token_none_for_unknown_expired_or_revoked(fake_conn: FakeConnection) -> None:
    # The query already filters expired/revoked rows: no row back means the
    # token is unknown, expired, or revoked — all indistinguishable, all None.
    fake_conn.fetchone_result = None
    assert sessions.validate_token("expired-or-revoked-token") is None


def test_validate_token_empty_token_skips_db(fake_conn: FakeConnection) -> None:
    assert sessions.validate_token("") is None
    assert fake_conn.executed == []


# ── revoke_token ────────────────────────────────────────────────────────


def test_revoke_token_updates_and_commits(fake_conn: FakeConnection) -> None:
    sessions.revoke_token("some-token")
    sql, params = fake_conn.executed[0]
    assert "SET revoked_at = now()" in sql
    assert params == ("some-token",)
    assert fake_conn.committed is True


def test_revoke_token_empty_is_noop(fake_conn: FakeConnection) -> None:
    sessions.revoke_token("")
    assert fake_conn.executed == []


# ── get_current_user (the FastAPI dependency) ───────────────────────────


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_get_current_user_401_without_credentials() -> None:
    with pytest.raises(HTTPException) as excinfo:
        sessions.get_current_user(credentials=None)
    assert excinfo.value.status_code == 401


def test_get_current_user_401_for_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "validate_token", lambda token: None)
    with pytest.raises(HTTPException) as excinfo:
        sessions.get_current_user(credentials=_bearer("expired-token"))
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "invalid_or_expired_token"


def test_get_current_user_403_for_inactive_account(monkeypatch: pytest.MonkeyPatch) -> None:
    suspended = {"id": 7, "email": "x@acme.example", "name": "X", "role": "member", "status": "suspended"}
    monkeypatch.setattr(sessions, "validate_token", lambda token: suspended)
    with pytest.raises(HTTPException) as excinfo:
        sessions.get_current_user(credentials=_bearer("valid-token"))
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "account_inactive"


def test_get_current_user_returns_active_user(monkeypatch: pytest.MonkeyPatch) -> None:
    active = {"id": 7, "email": "x@acme.example", "name": "X", "role": "member", "status": "active"}
    monkeypatch.setattr(sessions, "validate_token", lambda token: active)
    assert sessions.get_current_user(credentials=_bearer("valid-token")) == active
