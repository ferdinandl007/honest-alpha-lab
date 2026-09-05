"""One real CLI researcher through the supervisor; not a financial benchmark.

This deliberately runs one job, not the perpetual service. It uses the configured
Codex account, can consume model usage, and does not buy datasets or place trades.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from honest_alpha_lab.supervisor import CampaignSpec, ResearchSupervisor, SupervisorStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--allow-unmetered-provider", action="store_true",
                        help="acknowledge provider billing without enforced token/dollar caps")
    args = parser.parse_args()
    root = Path(args.output_root).resolve()
    store = SupervisorStore(root / "state.sqlite")
    store.register("public-proxy-smoke-v1", CampaignSpec("proposal", {
        "agent_kind": "alternative_dataset_creator", "prompt_name": "semantic-dataset-discovery",
        "brief": (
            "This is a short real-source collection smoke test. Within 60 seconds, fetch and save one "
            "small publicly available document or aggregate data sample from an official source such as "
            "EIA, Census or NOAA. Choose the source yourself; do not bypass a refusal. Write a small "
            "research script or parse a few meaningful fields. Preserve the source URL, exact bytes, "
            "retrieval timestamp and script in scratch. Explain one tentative economic proxy and which "
            "US-equity industry it might inform, with limitations. If fetching fails, honestly report "
            "that failure and the attempted source. Emit alpha_candidates=[]; put findings and absolute "
            "artifact paths in output.summary/output.notes. No backtest, accepted alpha, paid data or trade."
        ), "max_agent_tokens": 15000, "allow_unmetered_provider": args.allow_unmetered_provider,
    }, timeout_seconds=120, max_attempts=1, max_jobs_per_utc_day=1))
    result = ResearchSupervisor(store, root / "work").step()
    print(json.dumps({"job": result, "state": store.status()}, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
