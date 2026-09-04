"""Launch one auditable, SEC-only Text & Event proposal round with Codex CLI.

This is deliberately a *proposal* job.  It has no market-price data, registry
write capability, numerical evaluator, or sealed-test access.  The snapshot
identifies the source contract used for ideation; a later evaluation must bind
each surviving candidate to frozen EDGAR archive files and a PIT market-data
snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from honest_alpha_lab.cli_agents import CliAgentSpec, CliSubagentWorker, FileTaskStore  # noqa: E402
from honest_alpha_lab.contracts import (  # noqa: E402
    AgentKind,
    DataLineage,
    InputSnapshot,
    ResearchBudget,
    ResearchJob,
    canonical_hash,
)
from honest_alpha_lab.ledger import AlphaRegistry  # noqa: E402
from honest_alpha_lab.prompts import get_prompt  # noqa: E402
from honest_alpha_lab.research_loop import DiversityResearchLoop, ResearchRequest  # noqa: E402
from honest_alpha_lab.research_vault import ResearchVault  # noqa: E402
from honest_alpha_lab.subagents import SubagentOrchestrator  # noqa: E402


UTC = timezone.utc


def main() -> None:
    prompt = get_prompt("sec-edgar-event-mining")
    source_contract = {
        "source": "SEC EDGAR filings and XBRL",
        "availability_rule": "use the filing ACCEPTANCE-DATETIME in the immutable archive",
        "prohibited": [
            "simple guidance revision",
            "fundamental acceleration",
            "Form-4 cluster buying",
            "13D strategic intent",
            "8-K cybersecurity",
        ],
    }
    lineage = DataLineage(
        dataset_id="sec-edgar-filings",
        provider="U.S. Securities and Exchange Commission",
        license_id="public-edgar-access-terms",
        source_uri="https://www.sec.gov/edgar",
        retrieval_time=datetime.now(UTC),
        content_hash=canonical_hash(source_contract),
        schema_version="edgar-submission-and-xbrl-v1",
    )
    snapshot = InputSnapshot(
        dataset_hashes=(lineage.content_hash,),
        universe_hash="research-ideation-no-universe-yet",
        code_hash=canonical_hash({"prompt_hash": prompt.prompt_hash, "source": source_contract}),
    )
    job = ResearchJob(
        job_id=str(uuid4()),
        agent_kind=AgentKind.TEXT_EVENT,
        input_snapshot_hash=snapshot.snapshot_hash,
        budget=ResearchBudget(max_trials=5, max_runtime_seconds=900, max_agent_tokens=30_000),
        prompt_hash=prompt.prompt_hash,
        requested_by="research-orchestrator",
    )
    registry = AlphaRegistry()
    orchestrator = SubagentOrchestrator(registry)
    orchestrator.register_worker(
        CliSubagentWorker(
            AgentKind.TEXT_EVENT,
            CliAgentSpec.codex(timeout_seconds=900),
            FileTaskStore(ROOT / "var" / "agent-tasks"),
        )
    )
    vault = ResearchVault(ROOT / "research-vault")
    loop = DiversityResearchLoop(orchestrator, registry, vault)
    result = loop.run_round(
        (
            ResearchRequest(
                job=job,
                prompt_name=prompt.name,
                context={
                    "lineages": (lineage,),
                    "source_contract": source_contract,
                    "required_candidate_count": 5,
                },
            ),
        ),
        round_id=f"sec-edgar-event-{job.job_id}",
    )
    print(
        {
            "round_id": result.vault_round.round_id,
            "run_statuses": [run.status.value for run in result.runs],
            "candidate_ids": list(result.vault_round.candidate_ids),
            "vault_round": str(ROOT / "research-vault" / "rounds"),
        }
    )
    for candidate in result.candidates:
        print({"name": candidate.name, "specification": candidate.specification})


if __name__ == "__main__":
    main()
