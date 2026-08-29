"""
Live validation gate for the Power BI connection (Mission gate — run BEFORE
building anything on top of the adapter).

Usage (from the backend root, with your .env filled in):

    python3 scripts/validate_powerbi_auth.py

Walks the full chain a real /chat request depends on, stopping at the first
broken link with an ACTIONABLE diagnosis:

    env vars → token from Microsoft Entra → health check (list workspaces)
    → list semantic models → execute EVALUATE ROW("test", 1) on the dataset

Exit code 0 = every step passed; 1 = a step failed (the output says which and
what to check). Credentials are printed MASKED — never paste this output with
full identifiers into a ticket.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Standalone script: make the backend root importable, then use the same
# adapter code the app runs — validating a copy would validate nothing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app.adapters.powerbi import (  # noqa: E402
    PowerBIAPIError,
    PowerBIAuthenticator,
    PowerBIAuthError,
    PowerBIClient,
    PowerBIConfigError,
)

_MASK_KEEP = 8


def _mask(value: str) -> str:
    if not value:
        return "<not set>"
    return f"{value[:_MASK_KEEP]}... ({len(value)} chars)"


def _fail(step: str, causes: list[str]) -> int:
    print(f"\n[validate] FAILED at step: {step}")
    print("[validate] likely causes, in order of probability:")
    for cause in causes:
        print(f"  - {cause}")
    return 1


def main() -> int:
    print("[validate] Power BI connection gate\n")
    print("[validate] credentials (masked):")
    for name in (
        "POWERBI_TENANT_ID",
        "POWERBI_CLIENT_ID",
        "POWERBI_CLIENT_SECRET",
        "POWERBI_WORKSPACE_ID",
        "POWERBI_DATASET_ID",
    ):
        print(f"  {name} = {_mask(os.getenv(name, ''))}")
    print()

    # Step 1: construct the authenticator (env fail-fast) + acquire a token.
    try:
        authenticator = PowerBIAuthenticator()
        authenticator.get_token()
        print("[validate] step 1 OK: token acquired from Microsoft Entra")
    except PowerBIConfigError as exc:
        return _fail(
            "environment",
            [
                f"{exc} — copy .env.example to .env and fill every __SET_ME__",
            ],
        )
    except PowerBIAuthError as exc:
        return _fail(
            "token acquisition",
            [
                "POWERBI_CLIENT_SECRET is wrong or has EXPIRED (secrets have a "
                "max lifetime — check the app registration's certificates & secrets page)",
                "POWERBI_CLIENT_ID / POWERBI_TENANT_ID do not match the app registration",
                "network/proxy blocking login.microsoftonline.com",
                f"detail: {exc} | context: {exc.context}",
            ],
        )

    client = PowerBIClient(authenticator)

    # Step 2: health check — the token is real, but is it AUTHORIZED?
    if not client.health_check():
        return _fail(
            "health check (list workspaces)",
            [
                "the tenant has not enabled 'Service principals can use Fabric APIs' "
                "in the admin portal (the classic silent blocker)",
                "the service principal was never added to any workspace",
                "the token audience is wrong (scope must be the Power BI API)",
            ],
        )
    print("[validate] step 2 OK: API reachable and authorized")

    # Step 3: list models in the configured workspace.
    try:
        models = client.list_models()
    except PowerBIConfigError as exc:
        return _fail("list models", [str(exc)])
    except PowerBIAuthError as exc:
        return _fail(
            "list models",
            [
                "the service principal is not a member/viewer of THIS workspace "
                "(being valid tenant-wide is not enough — add it in workspace access)",
                f"detail: {exc} | context: {exc.context}",
            ],
        )
    except PowerBIAPIError as exc:
        return _fail(
            "list models",
            [
                "POWERBI_WORKSPACE_ID does not exist or is a personal workspace "
                "(personal workspaces are not addressable via /groups)",
                f"detail: {exc} | context: {exc.context}",
            ],
        )
    print(f"[validate] step 3 OK: {len(models)} semantic model(s) visible")
    for model in models:
        print(f"  - {model.name} (id {_mask(model.id)})")

    # Step 4: execute a trivial DAX query against the configured dataset —
    # the exact call the orchestrator's tool makes.
    dataset_id = os.getenv("POWERBI_DATASET_ID", "").strip()
    if not dataset_id:
        return _fail("execute query", ["POWERBI_DATASET_ID is not set"])
    try:
        result = client.execute_query(dataset_id, 'EVALUATE ROW("test", 1)')
    except PowerBIAuthError as exc:
        return _fail(
            "execute query",
            [
                "the service principal can SEE the workspace but lacks Build "
                "permission on this dataset",
                f"detail: {exc} | context: {exc.context}",
            ],
        )
    except PowerBIAPIError as exc:
        status = exc.context.get("status_code")
        causes = [f"detail: {exc} | context: {exc.context}"]
        if status == 404:
            causes.insert(
                0,
                "POWERBI_DATASET_ID does not match any model in this workspace "
                "(compare with the ids listed in step 3)",
            )
        elif status == 429:
            causes.insert(0, "throttled — wait for the Retry-After above and re-run")
        elif status and status >= 500:
            causes.insert(0, "Power BI capacity issue — usually transient, re-run")
        return _fail("execute query", causes)

    if result.row_count != 1 or result.rows[0].get("test") != 1:
        return _fail(
            "execute query",
            [f"unexpected result shape: columns={result.columns} rows={result.rows}"],
        )
    print("[validate] step 4 OK: EVALUATE ROW(\"test\", 1) returned 1 row")

    print("\n[validate] ALL STEPS PASSED — the adapter chain is live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
