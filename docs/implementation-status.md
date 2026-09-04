# Research-system implementation audit

Goal: implement the research-informed Honest Alpha Lab phase-1 system. No claim of
global optimality or profitable alpha; compare alternatives on reproducible evidence.

## Requirements and completion evidence

Latest integration: [state analysis and portfolio workflow](state-and-portfolio-workflow.md).
The two previously disconnected components now have end-to-end software paths:
both state methods appear in numerical reports and development feedback, and dated
numerical signals feed the conventional portfolio comparison command and recurring
supervisor jobs. Allocation weights are frozen before the internal test; holdings
rebalance on each sleeve's schedule. Automatic refitting of allocation weights
during a test is not implemented or implied.

This does not close the broader real-data approval, final-validation, deployment
or financial-benchmark requirements below. Historical checkpoints remain as an
audit trail, not a description of the latest implementation.

All unchecked items remain required, even if a smaller component passes its tests.

- [ ] Point-in-time ingestion: raw snapshots, licenses, vintages, dated universe and
  issuer/exposure mapping, corporate actions and delistings; real-data verification.
- [ ] Explicit date/security panel DSL with bounded parsing, missing-data semantics,
  tie-correct ranks, canonical deduplication and independent numerical tests.
- [ ] Executable symbolic, text/event and alternative-dataset creation paths with
  lawful source discovery, data-needed outcomes and mechanism-specific validation.
- [ ] Provider-neutral CLI agents with typed artifacts, enforced budgets, least
  privilege, durable queue/leases/retries and end-to-end measured feedback.
- [ ] Persistent immutable registry and all-trial ledger; authenticated approval
  boundaries, content-addressed artifacts, PostgreSQL production adapter.
- [ ] Purged walk-forward, incremental-library evaluation, horizon/decay/stability,
  multiple-testing uncertainty, costs and capacity diagnostics.
- [ ] Train-only conventional ridge/elastic-net and LightGBM models, equal ensemble;
  fitted Gaussian HMM filtered probabilities and non-Markov comparison.
- [ ] Sealed final test: atomic durable one-use enforcement plus deployment-level
  separation of data and authority; final outcomes excluded from search feedback.
- [ ] Shared graph of papers, hypotheses, ancestry, failures, datasets and measured
  duplicates, used by mining runs rather than only readable notes.
- [ ] Real-data benchmark arms with matched budgets, seed variation, ablations,
  rewards and cost/latency accounting; fixtures never represented as alpha results.
- [ ] Traditional portfolio construction and research-only execution with audited
  timestamp, missing-price, order, turnover and risk behavior.
- [ ] Documented reproducible deployment, tests, actual agent mining/evaluation run
  and requirement-by-requirement completion audit.

## Baseline verified at implementation start, 4 September 2026

25 existing tests pass. This is a component scaffold: proposals are recorded but
not connected to historical evaluation. The earlier review's rank, reward and
sealed-state gaps are still present. The existing 15 candidate records are not
validated strategies. No paid data subscription or live-trading authorization is
implied by this implementation goal. Sources under `sources/` remain read-only.

## Work in progress

The prior implementation turn made concrete progress. Its work was revalidated:
381 tests passed, including actual PostgreSQL role/concurrency/restart tests and
Parquet-to-command numerical execution. The interrupted test handle was no longer
present; no live numerical work was restarted on an assumed timeout.

Implemented and independently exercised components now include explicit panel DSL,
content-verified Parquet snapshots, conventional purged evaluation, fitted HMM and
non-Markov probabilities, durable one-use sealed reservations, development-only
reward/memory controls, and repaired portfolio accounting/execution cases.

Current integration adds `pipeline.py` and `QueuedResearchLoop`: canonical persistent
reservations, fixed-plan numerical subprocesses with timeouts and heartbeats,
immutable report/prediction artifacts, and measured next-round feedback. Their
integration tests use a real database/process boundary but correctly labeled
software fixtures. See [the persistent-loop guide](persistent-research-loop.md).

The full objective remains unproven. In particular: real provider data and independent
approval; event/proxy feature compilation; complete spend/compute budgets; atomic
coordinator recovery; graph-driven search policies; cost/portfolio/state integration;
benchmark comparisons and an actual real-data agent-mining run are not finished.
No profitable alpha, full deployment isolation or complete platform is claimed.

## Verified integration checkpoint

The complete suite after persistent-loop integration passed: **387 tests**, with
five upstream hmmlearn/NumPy deprecation warnings. Focused Ruff checks on changed
pipeline, loop, CLI, DSL and integration tests also passed. Machine-readable test
evidence: `var/verification/persistent-pipeline-2026-09-04.xml`.

This proves the covered software behavior, including real PostgreSQL and child
process integration. It does not prove any historical signal profitable, fulfill
the real-data benchmark requirement, or complete the unchecked platform scope.

Next implementation priorities remain: proposal-round durability and spend limits;
lawful data discovery and executable event/proxy features; library/graph-driven
search and cost-sensitive benchmark integration. The active goal is not complete.

## Event/proxy handoff and exploration checkpoint — 5 September 2026

Implemented `features.py`, the `build-features` command and an agent-callable
`EvidenceFeatureBuildTool`. Structured event scores and proxy measurements now
materialize into the numerical snapshot schema with source spans, content hashes,
publication/retrieval/extraction clocks, revisions/withdrawals, session lags,
expiry/decay, versioned asset exposures and per-row contribution provenance.
Missing inputs are not fabricated as zeros. Default as-run extraction is distinct
from explicitly opted-in retrospective LLM publication replay.

The full suite passed **415 tests**, zero failures/errors/skips, with the same five
upstream HMM warnings. Evidence: `var/verification/event-proxy-features-2026-09-05.xml`.
Focused lint checks also passed. The 28 new tests cover the handoff including
actual command → Parquet → immutable snapshot → DSL execution. They are software
fixtures, not alpha benchmarks or proof of extraction accuracy.

Following the user's explicit request for less rigid researchers, version-2
symbolic, event and alternative-dataset prompts permit open-ended discovery and
experimental code/collection when the relevant tools and isolated workspace are
provided. The catalog is no longer prescribed as the only discovery space. Strict
structured contracts apply at the numerical handoff, not to the agent's entire
research process. Permission to explore is not permission to approve, trade,
exceed budgets or obtain unauthorized data.

The full goal remains active. Safe collection tools and genuinely isolated
research execution still need implementation; prompt changes do not provide
them. Extraction semantic verification, authenticated data/map approval, legacy
mechanism-contract generalization, actual real-data mining/benchmarks, persistent
round recovery, full spend controls and the other unchecked integration items
remain outstanding. See [the feature handoff guide](event-proxy-features.md).

## Always-on supervisor checkpoint — 5 September 2026

The user's explicit requirement is a 24/7 signal-discovery service, not manually
triggered batches. `supervisor.py` now provides persistent recurring campaigns,
exclusive leases, retry state, pause/resume, daily UTC dispatch caps and continual
refill. It stays alive when queues are idle or daily dispatch allowances are spent.
POSIX worker process groups and an independent watchdog bound worker lifetimes
even after supervisor failure. SQLite stores single-host campaign state; numerical
trials continue to use PostgreSQL.

`campaign_workers.py` connects actual CLI researchers and existing numerical runs,
freezes job context, checkpoints proposals before submission, reuses completed
responses, and supplies recent findings to later researchers. The CLI researcher
factory now permits network-enabled scratch work and arbitrary collection/research
code; discovery-only responses do not require approved lineage. The optional HTTPS
collector is not a mandatory route. Separate OS/read isolation remains required.

The full suite passed **496 tests**, zero failures/errors/skips and five existing
upstream warnings. Evidence: `var/verification/supervisor-2026-09-05.xml`. Focused
lint checks passed. Tests include actual concurrent claims, worker deadlines,
supervisor-crash watchdog behavior, checkpoint recovery and research workspace
collection/code execution.

One real Codex job also completed through the supervisor and collected/parsed
30,328 bytes and 918 monthly observations from NOAA. Raw bytes and provenance were
independently checked. See [the live smoke report](../research/supervisor-live-smoke-2026-09-05.md).
It produced an unvalidated proxy hypothesis, not a backtested alpha. It exposed
the unresolved mismatch between agent-reported zero tokens and actual provider
usage, so unattended account-spend enforcement is not claimed.

The example three-role service can be started explicitly using the
[service guide](always-on-service.md). No perpetual service or host auto-start
installation was left running by the finite smoke test. Automatic startup,
health/alerts, global provider-based spending controls, numerical-run renewal,
full real-data validation/benchmarks, dataset admission, deeper research planning
and multi-day reliability certification remain required. The overall goal is
active and incomplete.
