"""
Unit tests for PowerBIAuthenticator (app/adapters/powerbi/auth.py).

httpx.post is monkeypatched — no request ever leaves the process. The suite
pins the three behaviors that matter operationally: fail-fast on missing env,
token caching with the 60s safety buffer, and typed errors for every failure
mode of the token endpoint.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from app.adapters.powerbi.auth import PowerBIAuthenticator
from app.adapters.powerbi.exceptions import PowerBIAuthError, PowerBIConfigError
from tests.conftest import FakeHTTPResponse

_ENV_VARS = ("POWERBI_TENANT_ID", "POWERBI_CLIENT_ID", "POWERBI_CLIENT_SECRET")


def _patch_token_post(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace httpx.post with a recorder returning a 200 token response."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, data: dict[str, str], timeout: float) -> FakeHTTPResponse:
        calls.append({"url": url, "data": data, "timeout": timeout})
        return FakeHTTPResponse(200, json_data=payload)

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


# ── Fail-fast construction ──────────────────────────────────────────────


@pytest.mark.parametrize("missing_var", _ENV_VARS)
def test_missing_env_var_fails_fast_and_names_it(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str], missing_var: str
) -> None:
    monkeypatch.delenv(missing_var)
    with pytest.raises(PowerBIConfigError) as excinfo:
        PowerBIAuthenticator()
    assert missing_var in str(excinfo.value)
    assert excinfo.value.context["missing"] == [missing_var]


def test_all_missing_env_vars_are_named_together(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(PowerBIConfigError) as excinfo:
        PowerBIAuthenticator()
    for var in _ENV_VARS:
        assert var in str(excinfo.value)


def test_whitespace_only_value_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str]
) -> None:
    monkeypatch.setenv("POWERBI_CLIENT_SECRET", "   ")
    with pytest.raises(PowerBIConfigError) as excinfo:
        PowerBIAuthenticator()
    assert "POWERBI_CLIENT_SECRET" in str(excinfo.value)


# ── Token acquisition + caching ─────────────────────────────────────────


def test_get_token_acquires_via_client_credentials(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str], token_payload: dict[str, Any]
) -> None:
    calls = _patch_token_post(monkeypatch, token_payload)
    token = PowerBIAuthenticator().get_token()

    assert token == "fake-access-token-abc123"
    assert len(calls) == 1
    assert powerbi_env["POWERBI_TENANT_ID"] in calls[0]["url"]
    sent = calls[0]["data"]
    assert sent["grant_type"] == "client_credentials"
    assert sent["client_id"] == powerbi_env["POWERBI_CLIENT_ID"]
    assert sent["client_secret"] == powerbi_env["POWERBI_CLIENT_SECRET"]
    assert "analysis.windows.net" in sent["scope"]


def test_valid_token_is_cached_across_calls(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str], token_payload: dict[str, Any]
) -> None:
    calls = _patch_token_post(monkeypatch, token_payload)
    authenticator = PowerBIAuthenticator()
    first = authenticator.get_token()
    second = authenticator.get_token()

    assert first == second
    assert len(calls) == 1  # the cache absorbed the second call


def test_expired_token_triggers_refresh(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str], token_payload: dict[str, Any]
) -> None:
    calls = _patch_token_post(monkeypatch, token_payload)
    authenticator = PowerBIAuthenticator()
    authenticator.get_token()

    # Simulate expiry: the cached token's clock has run out.
    authenticator._token_expires_at = time.time() - 1
    authenticator.get_token()
    assert len(calls) == 2


def test_60s_safety_buffer_refreshes_before_actual_expiry(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str], token_payload: dict[str, Any]
) -> None:
    calls = _patch_token_post(monkeypatch, token_payload)
    authenticator = PowerBIAuthenticator()
    authenticator.get_token()

    # 30s of technically-valid lifetime left, but inside the 60s buffer:
    # the token must NOT be used for a new request (it could die mid-flight).
    authenticator._token_expires_at = time.time() + 30
    authenticator.get_token()
    assert len(calls) == 2

    # Comfortably outside the buffer → cache hit, no third request.
    authenticator._token_expires_at = time.time() + 120
    authenticator.get_token()
    assert len(calls) == 2


# ── Failure modes of the token endpoint ─────────────────────────────────


def test_non_200_from_entra_raises_auth_error_with_context(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str]
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: FakeHTTPResponse(401, text='{"error": "invalid_client"}'),
    )
    with pytest.raises(PowerBIAuthError) as excinfo:
        PowerBIAuthenticator().get_token()
    assert "401" in str(excinfo.value)
    assert excinfo.value.context["status_code"] == 401
    assert "invalid_client" in excinfo.value.context["body"]


def test_network_error_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str]
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> FakeHTTPResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(PowerBIAuthError) as excinfo:
        PowerBIAuthenticator().get_token()
    assert "connection refused" in excinfo.value.context["error"]


def test_malformed_token_payload_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch, powerbi_env: dict[str, str]
) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: FakeHTTPResponse(200, json_data={"unexpected": "shape"})
    )
    with pytest.raises(PowerBIAuthError) as excinfo:
        PowerBIAuthenticator().get_token()
    assert "unexpected shape" in str(excinfo.value)
