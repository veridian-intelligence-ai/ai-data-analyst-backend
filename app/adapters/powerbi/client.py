"""
Minimal REST client for the Power BI API (works for Fabric workspaces too).

Implements the `BaseDataSourceAdapter` contract:
- `health_check()`  — verifies API access by listing visible workspaces.
- `list_models()`   — enumerates the semantic models of the workspace.
- `execute_query()` — runs a DAX query against one semantic model.

The error map below is production-hardened knowledge: each HTTP status the
executeQueries endpoint actually returns is translated into a typed exception
whose context carries exactly what the LLM self-correction loop needs (the
400 body contains Power BI's real DAX error text).
"""
from __future__ import annotations

import os
import time
from collections import Counter
from typing import Any

import httpx

from app.adapters.base import BaseDataSourceAdapter, QueryResult, SemanticModel
from app.adapters.powerbi.auth import PowerBIAuthenticator
from app.adapters.powerbi.exceptions import (
    PowerBIAPIError,
    PowerBIAuthError,
    PowerBIConfigError,
    PowerBIError,
)

_POWER_BI_BASE_URL = "https://api.powerbi.com/v1.0/myorg"


def _log_failure(operation: str, exc: PowerBIError) -> None:
    """One greppable stdout line per adapter failure, BEFORE the raise.

    The typed exception's context reaches the LLM as tool-result food, but
    operators diagnose from logs — a raise that leaves no line is invisible
    during an incident. Bulky context values (response payloads) are capped.
    """
    context = {k: str(v)[:300] for k, v in exc.context.items()}
    print(f"[powerbi.client] {operation} FAILED: {exc} | context={context}")
# DAX over a hot model is typically fast (sub-second to ~2s), but a cold
# capacity or a heavy query can take much longer. This must stay ABOVE the
# LLM-loop budget so the adapter never times out before the orchestrator.
_EXECUTE_QUERY_TIMEOUT_SECONDS = 120.0
_DEFAULT_TIMEOUT_SECONDS = 30.0


class PowerBIClient(BaseDataSourceAdapter):
    """Adapter for the Power BI REST API."""

    def __init__(self, authenticator: PowerBIAuthenticator) -> None:
        self._authenticator = authenticator

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._authenticator.get_token()}"}

    @staticmethod
    def _require_workspace_id() -> str:
        workspace_id = os.getenv("POWERBI_WORKSPACE_ID", "").strip()
        if not workspace_id:
            raise PowerBIConfigError(
                "POWERBI_WORKSPACE_ID not set",
                context={"missing": ["POWERBI_WORKSPACE_ID"]},
            )
        return workspace_id

    @staticmethod
    def _normalize_column_name(name: str) -> str:
        """
        Display-name candidate for a result column:
        - pure measure  '[Total Revenue]'          -> 'Total Revenue'
        - qualified col 'dim_products[category]'   -> 'category'
        - anything else                            -> unchanged

        This is only the *candidate*: collision handling lives in
        `_build_column_name_map`, which keeps the qualified names when two
        columns would collapse into the same leaf (never lose data silently).
        """
        if len(name) >= 2 and name.endswith("]"):
            open_idx = name.find("[")
            if open_idx != -1:
                inner = name[open_idx + 1 : -1]
                # Conservative: reject nested/escaped brackets.
                if inner and "[" not in inner and "]" not in inner:
                    return inner
        return name

    @classmethod
    def _build_column_name_map(cls, raw_columns: list[str]) -> dict[str, str]:
        """
        Map each raw result column to its display name, stripping the
        'table[column]' / '[Measure]' brackets.

        Collision guard: if two raw columns collapse to the same leaf name,
        BOTH keep their original qualified names. A verbose label is better
        than silently overwriting a column's data.
        """
        candidates = [cls._normalize_column_name(name) for name in raw_columns]
        counts = Counter(candidates)
        return {
            raw: (candidate if counts[candidate] == 1 else raw)
            for raw, candidate in zip(raw_columns, candidates, strict=True)
        }

    def health_check(self) -> bool:
        """Confirm API access by listing visible workspaces. Never raises."""
        url = f"{_POWER_BI_BASE_URL}/groups"
        try:
            response = httpx.get(
                url, headers=self._auth_headers(), timeout=_DEFAULT_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            print(f"[powerbi.client] health check FAILED: {exc}")
            return False
        except Exception as exc:  # includes PowerBIAuthError from the authenticator
            print(f"[powerbi.client] health check FAILED: {exc}")
            return False

        if response.status_code == 200:
            try:
                count = len(response.json().get("value", []))
            except ValueError:
                count = 0
            print(f"[powerbi.client] health check OK, {count} workspaces visible")
            return True

        print(f"[powerbi.client] health check FAILED: HTTP {response.status_code}")
        return False

    def list_models(self) -> list[SemanticModel]:
        """List the semantic models (datasets) of the configured workspace."""
        try:
            return self._list_models()
        except PowerBIError as exc:
            _log_failure("list_models", exc)
            raise

    def _list_models(self) -> list[SemanticModel]:
        workspace_id = self._require_workspace_id()
        url = f"{_POWER_BI_BASE_URL}/groups/{workspace_id}/datasets"

        try:
            response = httpx.get(
                url, headers=self._auth_headers(), timeout=_DEFAULT_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            raise PowerBIAPIError(
                "Network error calling the Power BI datasets endpoint",
                context={"error": str(exc)},
            ) from exc

        if response.status_code in (401, 403):
            raise PowerBIAuthError(
                f"Power BI rejected the token with status {response.status_code}",
                context={"status_code": response.status_code, "body": response.text[:500]},
            )
        if response.status_code != 200:
            raise PowerBIAPIError(
                f"Power BI datasets endpoint returned HTTP {response.status_code}",
                context={"status_code": response.status_code, "body": response.text[:500]},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PowerBIAPIError(
                "Power BI datasets response is not valid JSON",
                context={"error": str(exc)},
            ) from exc

        models: list[SemanticModel] = []
        for entry in payload.get("value", []) or []:
            if not isinstance(entry, dict):
                continue
            models.append(
                SemanticModel(
                    id=entry.get("id", ""),
                    name=entry.get("name", ""),
                    workspace_id=workspace_id,
                    metadata={k: v for k, v in entry.items() if k not in {"id", "name"}},
                )
            )
        print(f"[powerbi.client] list_models: {len(models)} models found")
        return models

    def execute_query(self, model_id: str, query: str) -> QueryResult:
        """Execute a DAX query against the given semantic model."""
        try:
            return self._execute_query(model_id, query)
        except PowerBIError as exc:
            _log_failure("execute_query", exc)
            raise

    def _execute_query(self, model_id: str, query: str) -> QueryResult:
        workspace_id = self._require_workspace_id()

        if not model_id or not model_id.strip():
            raise PowerBIConfigError("model_id is required", context={"missing": ["model_id"]})
        if not query or not query.strip():
            raise PowerBIConfigError(
                "dax query is required and cannot be empty", context={"missing": ["query"]}
            )

        url = (
            f"{_POWER_BI_BASE_URL}/groups/{workspace_id}/datasets/"
            f"{model_id}/executeQueries"
        )
        body: dict[str, Any] = {
            "queries": [{"query": query}],
            "serializerSettings": {"includeNulls": True},
        }
        query_preview = query.strip()[:200]

        # The full DAX goes to stdout BEFORE execution: when a query fails or
        # hangs, the logs must already show exactly what was sent — the
        # 200-char preview in exception context is for the LLM, not for you.
        print(f"[powerbi.client] execute_query model={model_id} dax:\n{query.strip()}")

        start = time.monotonic()
        try:
            response = httpx.post(
                url,
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json=body,
                timeout=_EXECUTE_QUERY_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise PowerBIAPIError(
                "Network error calling the Power BI executeQueries endpoint",
                context={"error": str(exc), "model_id": model_id},
            ) from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        status = response.status_code

        # ── The production-hardened error map ──────────────────────────
        if status == 400:
            # The body carries Power BI's real DAX error message — this is
            # what the LLM reads to self-correct. Keep it.
            raise PowerBIAPIError(
                "DAX query invalid",
                context={
                    "status_code": status,
                    "body": response.text[:1000],
                    "model_id": model_id,
                    "dax_query": query_preview,
                },
            )
        if status in (401, 403):
            raise PowerBIAuthError(
                f"Power BI rejected the token with status {status}",
                context={"status_code": status, "body": response.text[:500]},
            )
        if status == 404:
            raise PowerBIAPIError(
                "Semantic model not found",
                context={
                    "status_code": status,
                    "model_id": model_id,
                    "workspace_id": workspace_id,
                },
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            ctx: dict[str, Any] = {"status_code": status}
            if retry_after is not None:
                ctx["retry_after"] = retry_after
            raise PowerBIAPIError("Throttled by Power BI", context=ctx)
        if 500 <= status < 600:
            raise PowerBIAPIError(
                "Power BI internal error",
                context={"status_code": status, "body": response.text[:500]},
            )
        if status != 200:
            raise PowerBIAPIError(
                f"Power BI executeQueries returned HTTP {status}",
                context={"status_code": status, "body": response.text[:500]},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PowerBIAPIError(
                "Power BI executeQueries response is not valid JSON",
                context={"error": str(exc)},
            ) from exc

        try:
            tables = payload["results"][0]["tables"]
            raw_rows = tables[0]["rows"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PowerBIAPIError(
                "Malformed Power BI executeQueries response",
                context={"response": payload, "error": str(exc)},
            ) from exc

        if not isinstance(raw_rows, list):
            raise PowerBIAPIError(
                "Malformed Power BI executeQueries response: rows is not a list",
                context={"response": payload},
            )

        if raw_rows:
            first_row = raw_rows[0]
            if not isinstance(first_row, dict):
                raise PowerBIAPIError(
                    "Malformed Power BI executeQueries response: row is not a dict",
                    context={"response": payload},
                )
            raw_columns = list(first_row.keys())
        else:
            raw_columns = []

        name_map = self._build_column_name_map(raw_columns)
        columns = [name_map[name] for name in raw_columns]
        normalized_rows: list[dict[str, Any]] = [
            {name_map.get(k, k): v for k, v in row.items()}
            for row in raw_rows
            if isinstance(row, dict)
        ]

        result = QueryResult(
            columns=columns,
            rows=normalized_rows,
            row_count=len(normalized_rows),
            metadata={
                "model_id": model_id,
                "dax_query": query_preview,
                "latency_ms": latency_ms,
            },
        )
        print(
            f"[powerbi.client] execute_query: {result.row_count} rows in {latency_ms}ms"
        )
        return result
