"""Run one bounded, proposal-only public alternative-data discovery round.

This is deliberately a discovery launcher: it provides a source catalog and creates
immutable proposed candidates, but neither downloads data nor evaluates a signal.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import argparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from honest_alpha_lab.agents import make_job
from honest_alpha_lab.cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore
from honest_alpha_lab.contracts import AgentKind, DataLineage, InputSnapshot, ResearchBudget, canonical_hash
from honest_alpha_lab.ledger import AlphaRegistry
from honest_alpha_lab.research_loop import DiversityResearchLoop, ResearchRequest
from honest_alpha_lab.research_vault import ResearchVault
from honest_alpha_lab.subagents import SubagentOrchestrator


UTC = timezone.utc
CATALOG_ID = "public-alternative-data-catalog"

OFFICIAL_SOURCES = (
    {
        "name": "EIA Weekly Petroleum Status Report",
        "dataset_id": "eia-wpsr",
        "uri": "https://www.eia.gov/petroleum/supply/weekly/",
        "availability": "Use the report release timestamp; selected tables are scheduled after 10:30 a.m. ET Wednesday.",
    },
    {
        "name": "BTS Freight Transportation Services Index",
        "dataset_id": "bts-freight-tsi",
        "uri": "https://www.bts.gov/tsi",
        "availability": "Use each archived BTS release timestamp and its stated reference month; never backfill revised values.",
    },
    {
        "name": "TSA checkpoint travel numbers",
        "dataset_id": "tsa-checkpoint-volumes",
        "uri": "https://www.tsa.gov/travel/passenger-volumes",
        "availability": "Use a first-observed daily snapshot timestamp; later page corrections are not available to historical features.",
    },
    {
        "name": "Census Advance Monthly Retail Trade",
        "dataset_id": "census-retail-releases",
        "uri": "https://www.census.gov/retail/marts/historic_releases.html",
        "availability": "Use the timestamp and values from the contemporaneous release, including its then-known seasonal adjustment vintage.",
    },
    {
        "name": "NOAA GHCN Daily",
        "dataset_id": "noaa-ghcn-daily",
        "uri": "https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily",
        "availability": "Only use observations present in a date-stamped first-seen archive; stations can report late and observations can be revised.",
    },
    {
        "name": "FAA operations data",
        "dataset_id": "faa-operations",
        "uri": "https://www.faa.gov/air_traffic/by_the_numbers",
        "availability": "Use the dated FAA publication vintage and distinguish preliminary from final/revised values.",
    },
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-unmetered-provider", action="store_true",
                        help="acknowledge provider billing without enforced token/dollar caps")
    args = parser.parse_args()
    now = datetime.now(UTC)
    catalog_hash = canonical_hash(OFFICIAL_SOURCES)
    snapshot = InputSnapshot(
        dataset_hashes=(catalog_hash,),
        universe_hash=canonical_hash("liquid-us-large-mid-cap-v1"),
        code_hash=canonical_hash("public-alternative-discovery-launcher-v1"),
    )
    lineage = DataLineage(
        dataset_id=CATALOG_ID,
        provider="Honest Alpha Lab public-source catalog",
        license_id="public-source-review-required",
        source_uri="https://www.eia.gov/petroleum/supply/weekly/",
        retrieval_time=now,
        content_hash=catalog_hash,
        schema_version="1",
    )
    request_text = (
        "Act as Honest Alpha Lab's Alternative Dataset Creation Agent. Use only the six official public "
        "aggregate sources listed in `official_sources`. No PII, scraped consumer data, or paid datasets. "
        "Propose exactly five distinct economic proxy hypotheses. Each alpha_candidates item must have a "
        "name and a specification with: official_source, release_timestamp_or_availability_lag, "
        "source_key_definition, exposure_kind, dated_mapping_evidence, affected_asset_rule, causal_chain "
        "(observation -> economic variable -> earnings surprise -> residual return), point_in_time_feature_rule, "
        "cost, and falsification. Every candidate must set "
        "lineage_ids to [public-alternative-data-catalog]. Do not claim validation and do not recommend trades."
    )
    job = make_job(
        AgentKind.ALTERNATIVE_DATASET_CREATOR,
        snapshot,
        ResearchBudget(max_trials=5, max_runtime_seconds=900, max_data_cost_usd=0.0, max_agent_tokens=20_000),
        request_text,
        requested_by="public-alternative-round-launcher",
    )
    registry = AlphaRegistry()
    orchestrator = SubagentOrchestrator(registry)
    orchestrator.register_worker(
        CliSubagentWorker(
            AgentKind.ALTERNATIVE_DATASET_CREATOR,
            CliAgentSpec.codex(timeout_seconds=900, allow_unmetered_provider=args.allow_unmetered_provider),
            FileTaskStore(ROOT / "var" / "agent-tasks"),
        )
    )
    loop = DiversityResearchLoop(orchestrator, registry, ResearchVault(ROOT / "research-vault"))
    result = loop.run_round(
        (
            ResearchRequest(
                job=job,
                prompt_name="semantic-dataset-discovery",
                context={
                    "lineages": (lineage,),
                    "lineage_ids": (CATALOG_ID,),
                    "official_sources": OFFICIAL_SOURCES,
                    "task_request": request_text,
                },
            ),
        ),
        round_id=f"public-alternative-{now.strftime('%Y%m%dT%H%M%SZ')}",
    )
    print(json.dumps({
        "round_id": result.vault_round.round_id,
        "runs": [{"task_id": run.task_id, "status": run.status.value, "error": run.error} for run in result.runs],
        "candidates": [
            {"candidate_id": candidate.candidate_id, "name": candidate.name, "specification": candidate.specification}
            for candidate in result.candidates
        ],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
