"""
Live validation gate for the full NL→DAX loop (LLM + guard + Power BI).

Usage (from the backend root, with .env filled in — spends real Anthropic
tokens and one or more Power BI queries):

    python3 scripts/validate_dax_generation.py
    python3 scripts/validate_dax_generation.py "Which category sold most last quarter?"

Runs ONE question through the real orchestrator and prints the per-iteration
observability table — latency, stop reason, token and CACHE counters. The
cache columns are the point: on a second consecutive run you should see
cache_read ≈ the big system block's size. If cache_read stays 0, the prompt
caching that funds the whole architecture is broken (a changing system block,
usually a date that leaked into it).

Exit code 0 = the loop produced an answer without an error; 1 otherwise.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import anthropic  # noqa: E402

from app.adapters.powerbi import PowerBIAuthenticator, PowerBIClient  # noqa: E402
from app.ai.orchestrator import DAXConversationOrchestrator  # noqa: E402

_DEFAULT_QUESTION = "What was total revenue last month?"


def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_QUESTION

    dataset_id = os.getenv("POWERBI_DATASET_ID", "").strip()
    if not dataset_id:
        print("[validate] POWERBI_DATASET_ID is not set — fill .env first")
        return 1
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        print("[validate] ANTHROPIC_API_KEY is not set — fill .env first")
        return 1

    print(f"[validate] question: {question!r}\n")

    orchestrator = DAXConversationOrchestrator(
        anthropic_client=anthropic.Anthropic(timeout=85.0),
        powerbi_client=PowerBIClient(PowerBIAuthenticator()),
        dataset_id=dataset_id,
    )
    result = orchestrator.run(question)

    # ── Per-iteration observability table ───────────────────────────────
    header = (
        f"{'it':>2} | {'latency':>8} | {'stop_reason':<12} | {'in':>7} | "
        f"{'out':>6} | {'cache_w':>8} | {'cache_r':>8} | {'dax_ok':>6} | {'rows':>5}"
    )
    print(header)
    print("-" * len(header))
    for it in result.iterations:
        dax_ok = "-" if it.dax_ok is None else str(it.dax_ok)
        rows = "-" if it.dax_row_count is None else str(it.dax_row_count)
        print(
            f"{it.iteration:>2} | {it.latency_ms:>6}ms | {it.stop_reason:<12} | "
            f"{it.input_tokens:>7} | {it.output_tokens:>6} | "
            f"{it.cache_write_tokens:>8} | {it.cache_read_tokens:>8} | "
            f"{dax_ok:>6} | {rows:>5}"
        )
        if it.dax_error:
            print(f"     dax_error: {it.dax_error[:200]}")

    print(
        f"\n[validate] totals: {result.total_latency_ms}ms | "
        f"in={result.total_input_tokens} out={result.total_output_tokens} | "
        f"cache_write={result.total_cache_write_tokens} "
        f"cache_read={result.total_cache_read_tokens} | "
        f"prompt v{result.prompt_version}"
    )
    if result.total_cache_read_tokens == 0 and len(result.iterations) > 1:
        print(
            "[validate] WARNING: zero cache reads across a multi-iteration turn — "
            "prompt caching is not landing; check that nothing volatile (dates!) "
            "is inside the cached system block"
        )

    print(f"\n[validate] dax_query: {result.dax_query}")
    print(f"[validate] answer:\n{result.answer}\n")
    if result.next_step_suggestions:
        print("[validate] suggestions:")
        for suggestion in result.next_step_suggestions:
            print(f"  - {suggestion}")

    if result.error:
        print(f"\n[validate] FAILED: {result.error_type}: {result.error}")
        return 1
    if not result.answer.strip():
        print("\n[validate] FAILED: empty answer")
        return 1
    print("\n[validate] PASSED — full NL→DAX loop is live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
