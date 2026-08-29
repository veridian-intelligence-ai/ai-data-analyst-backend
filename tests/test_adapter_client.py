"""
Unit tests for PowerBIClient (app/adapters/powerbi/client.py).

httpx.get/httpx.post are monkeypatched; the authenticator is a stub. The
most valuable assertions here are the ERROR MAP ones: each HTTP status the
executeQueries endpoint returns must become a typed exception whose context
carries what the LLM self-correction loop needs (especially the 400 body).
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.powerbi.client import PowerBIClient
from app.adapters.powerbi.exceptions import (
    PowerBIAPIError,
    PowerBIAuthError,
    PowerBIConfigError,
)
from tests.conftest import FakeHTTPResponse


class StubAuthenticator:
    """Duck-typed authenticator: returns a fixed token, or raises."""

    def __init__(self, token: str = "fake-access-token-abc123", raises: Exception | None = None):
        self._token = token
        self._raises = raises

    def get_token(self) -> str:
        if self._raises is not None:
            raise self._raises
        return self._token


@pytest.fixture
def client() -> PowerBIClient:
    return PowerBIClient(StubAuthenticator())


def _patch_get(monkeypatch: pytest.MonkeyPatch, response: FakeHTTPResponse) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeHTTPResponse:
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return response

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


def _patch_post(monkeypatch: pytest.MonkeyPatch, response: FakeHTTPResponse) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, headers: dict[str, str], json: Any, timeout: float) -> FakeHTTPResponse:
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def _rows_response(rows: list[dict[str, Any]]) -> FakeHTTPResponse:
    return FakeHTTPResponse(200, json_data={"results": [{"tables": [{"rows": rows}]}]})


# ── health_check ────────────────────────────────────────────────────────


def test_health_check_ok_and_sends_bearer_header(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient
) -> None:
    calls = _patch_get(monkeypatch, FakeHTTPResponse(200, json_data={"value": [{}, {}]}))
    assert client.health_check() is True
    assert calls[0]["headers"]["Authorization"] == "Bearer fake-access-token-abc123"
    assert calls[0]["url"].endswith("/groups")


def test_health_check_false_on_http_error_status(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient
) -> None:
    _patch_get(monkeypatch, FakeHTTPResponse(403, text="forbidden"))
    assert client.health_check() is False


def test_health_check_false_on_network_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient
) -> None:
    def fake_get(*args: Any, **kwargs: Any) -> FakeHTTPResponse:
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "get", fake_get)
    assert client.health_check() is False


def test_health_check_false_when_authenticator_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # health_check must never raise — even when token acquisition itself dies.
    failing = PowerBIClient(StubAuthenticator(raises=PowerBIAuthError("token acquisition failed")))
    assert failing.health_check() is False


# ── list_models ─────────────────────────────────────────────────────────


def test_list_models_shapes(
    monkeypatch: pytest.MonkeyPatch,
    client: PowerBIClient,
    powerbi_env: dict[str, str],
    fake_workspace_id: str,
) -> None:
    payload = {
        "value": [
            {"id": "model-1", "name": "Sales Analytics", "isRefreshable": True},
            {"id": "model-2", "name": "Finance"},
        ]
    }
    calls = _patch_get(monkeypatch, FakeHTTPResponse(200, json_data=payload))
    models = client.list_models()

    assert calls[0]["url"].endswith(f"/groups/{fake_workspace_id}/datasets")
    assert [m.id for m in models] == ["model-1", "model-2"]
    assert models[0].name == "Sales Analytics"
    assert models[0].workspace_id == fake_workspace_id
    # id/name live on the dataclass; everything else lands in metadata.
    assert models[0].metadata == {"isRefreshable": True}


def test_list_models_requires_workspace_id(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient
) -> None:
    monkeypatch.delenv("POWERBI_WORKSPACE_ID", raising=False)
    with pytest.raises(PowerBIConfigError) as excinfo:
        client.list_models()
    assert "POWERBI_WORKSPACE_ID" in str(excinfo.value)


def test_list_models_401_maps_to_auth_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_get(monkeypatch, FakeHTTPResponse(401, text="unauthorized"))
    with pytest.raises(PowerBIAuthError):
        client.list_models()


# ── execute_query: request construction ─────────────────────────────────


def test_execute_query_request_body_and_headers(
    monkeypatch: pytest.MonkeyPatch,
    client: PowerBIClient,
    powerbi_env: dict[str, str],
    fake_workspace_id: str,
) -> None:
    calls = _patch_post(monkeypatch, _rows_response([{"[Revenue]": 1250.5}]))
    dax = 'EVALUATE ROW("Revenue", [Total Revenue])'
    client.execute_query("model-1", dax)

    call = calls[0]
    assert call["url"].endswith(f"/groups/{fake_workspace_id}/datasets/model-1/executeQueries")
    assert call["headers"]["Authorization"] == "Bearer fake-access-token-abc123"
    assert call["headers"]["Content-Type"] == "application/json"
    # The exact executeQueries contract: one query + includeNulls (without it,
    # a null cell silently DROPS the key from that row's dict).
    assert call["json"] == {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }


def test_execute_query_rejects_empty_inputs(
    client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    with pytest.raises(PowerBIConfigError):
        client.execute_query("", "EVALUATE ROW(\"x\", 1)")
    with pytest.raises(PowerBIConfigError):
        client.execute_query("model-1", "   ")


# ── execute_query: the full error map ───────────────────────────────────


def test_400_carries_body_and_dax_previews(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    dax_error_body = '{"error": {"message": "Column [foo] cannot be found"}}' + "x" * 2000
    _patch_post(monkeypatch, FakeHTTPResponse(400, text=dax_error_body))
    long_dax = "EVALUATE SUMMARIZECOLUMNS(" + "products[category], " * 30 + "[Revenue])"

    with pytest.raises(PowerBIAPIError) as excinfo:
        client.execute_query("model-1", long_dax)

    ctx = excinfo.value.context
    assert ctx["status_code"] == 400
    # Body preview capped at 1000 chars and keeps the real DAX error text —
    # this is exactly what the LLM reads to self-correct.
    assert "Column [foo] cannot be found" in ctx["body"]
    assert len(ctx["body"]) <= 1000
    # DAX preview capped at 200 chars.
    assert ctx["dax_query"] == long_dax[:200]


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_map_to_auth_error(
    monkeypatch: pytest.MonkeyPatch,
    client: PowerBIClient,
    powerbi_env: dict[str, str],
    status: int,
) -> None:
    _patch_post(monkeypatch, FakeHTTPResponse(status, text="denied"))
    with pytest.raises(PowerBIAuthError) as excinfo:
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")
    assert excinfo.value.context["status_code"] == status


def test_404_context_names_model_and_workspace(
    monkeypatch: pytest.MonkeyPatch,
    client: PowerBIClient,
    powerbi_env: dict[str, str],
    fake_workspace_id: str,
) -> None:
    _patch_post(monkeypatch, FakeHTTPResponse(404, text="not found"))
    with pytest.raises(PowerBIAPIError) as excinfo:
        client.execute_query("missing-model", "EVALUATE ROW(\"x\", 1)")
    assert excinfo.value.context["model_id"] == "missing-model"
    assert excinfo.value.context["workspace_id"] == fake_workspace_id


def test_429_context_includes_retry_after_when_present(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_post(monkeypatch, FakeHTTPResponse(429, headers={"Retry-After": "42"}))
    with pytest.raises(PowerBIAPIError) as excinfo:
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")
    assert excinfo.value.context["retry_after"] == "42"

    _patch_post(monkeypatch, FakeHTTPResponse(429))
    with pytest.raises(PowerBIAPIError) as excinfo:
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")
    assert "retry_after" not in excinfo.value.context


def test_5xx_maps_to_api_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_post(monkeypatch, FakeHTTPResponse(503, text="capacity unavailable"))
    with pytest.raises(PowerBIAPIError) as excinfo:
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")
    assert excinfo.value.context["status_code"] == 503


def test_network_error_maps_to_api_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> FakeHTTPResponse:
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(PowerBIAPIError):
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")


def test_malformed_json_body_maps_to_api_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_post(monkeypatch, FakeHTTPResponse(200, text="<html>gateway</html>", raise_json=True))
    with pytest.raises(PowerBIAPIError) as excinfo:
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")
    assert "not valid JSON" in str(excinfo.value)


def test_malformed_result_shape_maps_to_api_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_post(monkeypatch, FakeHTTPResponse(200, json_data={"results": []}))
    with pytest.raises(PowerBIAPIError) as excinfo:
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")
    assert "Malformed" in str(excinfo.value)


def test_rows_not_a_list_maps_to_api_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_post(
        monkeypatch,
        FakeHTTPResponse(200, json_data={"results": [{"tables": [{"rows": "oops"}]}]}),
    )
    with pytest.raises(PowerBIAPIError):
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")


# ── execute_query: column normalization ─────────────────────────────────


def test_column_normalization_strips_brackets(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    rows = [
        {"sales[month]": "2026-01", "[Total Revenue]": 1000.0},
        {"sales[month]": "2026-02", "[Total Revenue]": 1100.0},
    ]
    _patch_post(monkeypatch, _rows_response(rows))
    result = client.execute_query("model-1", "EVALUATE ...")

    assert result.columns == ["month", "Total Revenue"]
    assert result.rows[0] == {"month": "2026-01", "Total Revenue": 1000.0}
    assert result.row_count == 2


def test_column_collision_keeps_qualified_names(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    # Both leaves are 'name': collapsing them would silently overwrite one
    # column's data. Both must keep their qualified originals.
    rows = [{"products[name]": "Widget", "stores[name]": "Downtown", "[Revenue]": 5.0}]
    _patch_post(monkeypatch, _rows_response(rows))
    result = client.execute_query("model-1", "EVALUATE ...")

    assert result.columns == ["products[name]", "stores[name]", "Revenue"]
    assert result.rows[0]["products[name]"] == "Widget"
    assert result.rows[0]["stores[name]"] == "Downtown"


def test_empty_rows_yield_empty_result(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_post(monkeypatch, _rows_response([]))
    result = client.execute_query("model-1", "EVALUATE ...")
    assert result.columns == []
    assert result.rows == []
    assert result.row_count == 0
    assert result.metadata["model_id"] == "model-1"


# ── Normalization guard rails and remaining error branches ──────────────


def test_normalize_column_name_leaves_nested_brackets_unchanged() -> None:
    # Conservative guard: anything beyond the simple table[column] shape is
    # returned verbatim rather than guessed at — a refactor that starts
    # "helpfully" parsing nested brackets would silently rename columns.
    assert PowerBIClient._normalize_column_name("weird[a][b]") == "weird[a][b]"
    assert PowerBIClient._normalize_column_name("sales[amount]") == "amount"
    assert PowerBIClient._normalize_column_name("[Revenue]") == "Revenue"
    assert PowerBIClient._normalize_column_name("plain") == "plain"


def test_list_models_5xx_maps_to_api_error(
    monkeypatch: pytest.MonkeyPatch, client: PowerBIClient, powerbi_env: dict[str, str]
) -> None:
    _patch_get(monkeypatch, FakeHTTPResponse(500, text="internal error"))
    with pytest.raises(PowerBIAPIError) as excinfo:
        client.list_models()
    assert excinfo.value.context["status_code"] == 500


def test_execute_query_failure_leaves_a_log_line(
    monkeypatch: pytest.MonkeyPatch,
    client: PowerBIClient,
    powerbi_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every adapter failure must print one greppable line BEFORE the raise —
    # the typed exception feeds the LLM, the log line feeds the operator.
    _patch_post(monkeypatch, FakeHTTPResponse(500, text="boom"))
    with pytest.raises(PowerBIAPIError):
        client.execute_query("model-1", "EVALUATE ROW(\"x\", 1)")
    out = capsys.readouterr().out
    assert "[powerbi.client] execute_query FAILED:" in out
    # The full DAX was printed before execution.
    assert 'EVALUATE ROW("x", 1)' in out
