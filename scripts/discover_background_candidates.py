#!/usr/bin/env python3
"""Bounded, metadata-only Pexels/Pixabay background discovery worker."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.background_candidates import (
    DISCOVERY_MAX_API_REQUESTS_PER_RUN,
    DISCOVERY_MAX_CANDIDATES_PER_RUN,
    DISCOVERY_MAX_METADATA_ITEMS_PER_RUN,
    DISCOVERY_MAX_SEARCH_WORKERS,
    DISCOVERY_VERIFIED_TARGET,
    discover_candidates,
)
from engine.background_pool import make_provider
from engine.supabase_state import SupabaseConfig, SupabaseStateStore
from scripts.build_verified_background_pool import _validate_environment, auto_load_env_if_needed

def run_discovery_step(
    *,
    store: Optional[SupabaseStateStore] = None,
    provider: str = "both",
    max_candidates: int = DISCOVERY_MAX_CANDIDATES_PER_RUN,
    target_pending: Optional[int] = None,
    verified_target: int = DISCOVERY_VERIFIED_TARGET,
    max_api_requests: int = DISCOVERY_MAX_API_REQUESTS_PER_RUN,
    max_metadata_items: int = DISCOVERY_MAX_METADATA_ITEMS_PER_RUN,
    max_search_workers: int = DISCOVERY_MAX_SEARCH_WORKERS,
    providers: Optional[dict[str, Any]] = None,
    logger: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Execute a single bounded discovery step using configured providers."""
    auto_load_env_if_needed()
    _validate_environment(provider)
    state_store = store if store is not None else SupabaseStateStore(SupabaseConfig.from_env())
    active_providers = (
        providers
        if providers is not None
        else {
            name: make_provider(name)
            for name in (("pexels", "pixabay") if provider == "both" else (provider,))
        }
    )
    limits = {
        "max_candidates": max_candidates,
        "max_api_requests": min(max_api_requests, DISCOVERY_MAX_API_REQUESTS_PER_RUN),
        "max_metadata_items": min(max_metadata_items, DISCOVERY_MAX_METADATA_ITEMS_PER_RUN),
    }
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise ValueError("Discovery limits must be positive integers")

    # Reserve part of the same per-run budget for each of three consecutive
    # categories. Each invocation advances the shared durable category cursor.
    # This makes Mac and GitHub use exactly the same bounded search code.
    turns = min(3, *limits.values())
    if turns == 1:
        return discover_candidates(
            store=state_store, providers=active_providers, max_candidates=max_candidates,
            target_pending=target_pending, verified_target=verified_target,
            max_api_requests=limits["max_api_requests"],
            max_metadata_items=limits["max_metadata_items"],
            max_search_workers=max_search_workers, logger=logger,
        )

    result = None
    totals = {key: 0 for key in (
        "discovered", "eligible_pending", "metadata_ineligible", "metadata_records_examined",
        "landscape_excluded", "duplicates", "provider_requests", "provider_errors",
        "portrait_candidates", "square_candidates", "high_res_landscape_candidates",
    )}
    queries: list[str] = []
    pages: list[int] = []
    categories: list[str] = []
    for index in range(turns):
        remaining_turns = turns - index
        budget = {key: (value + remaining_turns - 1) // remaining_turns
                  for key, value in limits.items()}
        result = discover_candidates(
            store=state_store, providers=active_providers,
            max_candidates=budget["max_candidates"], target_pending=target_pending,
            verified_target=verified_target,
            max_api_requests=budget["max_api_requests"],
            max_metadata_items=budget["max_metadata_items"],
            max_search_workers=max_search_workers, logger=logger,
        )
        for key in totals:
            totals[key] += result.get(key, 0)
        for query in result.get("query_terms_touched", []):
            if query not in queries:
                queries.append(query)
        for page in result.get("pages_touched", []):
            if page not in pages:
                pages.append(page)
        category = result.get("rotation", {}).get("category")
        if category:
            categories.append(category)
        limits["max_candidates"] -= result.get("discovered", 0)
        limits["max_api_requests"] -= result.get("provider_requests", 0)
        limits["max_metadata_items"] -= result.get("metadata_records_examined", 0)
        if (result.get("status") != "COMPLETED"
                or min(limits.values()) < 1):
            break
    assert result is not None
    return {**result, **totals, "query_terms_touched": queries,
            "pages_touched": pages, "categories_visited": categories}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enqueue bounded background metadata candidates; no downloads or CV scans.")
    parser.add_argument("--max-candidates", type=int, default=DISCOVERY_MAX_CANDIDATES_PER_RUN)
    parser.add_argument("--target-pending", type=int, default=None,
                        help="Optional explicit pending-queue ceiling (off by default).")
    parser.add_argument("--verified-target", type=int, default=DISCOVERY_VERIFIED_TARGET)
    parser.add_argument("--max-api-requests", type=int, default=DISCOVERY_MAX_API_REQUESTS_PER_RUN)
    parser.add_argument("--max-metadata-items", type=int, default=DISCOVERY_MAX_METADATA_ITEMS_PER_RUN)
    parser.add_argument("--max-search-workers", type=int, default=DISCOVERY_MAX_SEARCH_WORKERS)
    parser.add_argument("--provider", choices=("both", "pexels", "pixabay"), default="both")
    args = parser.parse_args(argv)
    try:
        result = run_discovery_step(
            provider=args.provider,
            max_candidates=args.max_candidates,
            target_pending=args.target_pending,
            verified_target=args.verified_target,
            max_api_requests=args.max_api_requests,
            max_metadata_items=args.max_metadata_items,
            max_search_workers=args.max_search_workers,
        )
        print(
            "BACKGROUND_DISCOVERY "
            f"status={result['status']} "
            f"eligible_pending={result.get('eligible_pending', result.get('discovered', 0))} "
            f"metadata_ineligible={result.get('metadata_ineligible', 0)} "
            f"metadata_examined={result.get('metadata_records_examined', 0)} "
            f"provider_requests={result.get('provider_requests', 0)}"
        )
        return 0
    except Exception as err:
        print(f"BACKGROUND_DISCOVERY_FAILED:{type(err).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
