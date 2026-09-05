# Money-safety review and migration

This is a software-correctness review, not proof of profitable or safe trading.
The review began against `e031b5f` on 5 September 2026. No real broker account,
provider billing account or live order was used. Local regression fixtures are
not financial benchmarks.

## Corrected paths

| Area | Defect and correction |
| --- | --- |
| Daily backtests | A later intraday protective exit could override a limit executable at the open. Execution now respects the known opening sequence before conservative intraday ordering. |
| Insolvency | Nonpositive NAV previously depended on the rebalance date. It now fails every day; paper ingestion must roll back rather than commit an ordinary successful day. |
| Signal age | Re-dating composites hid stale components. Each source component must satisfy both its declared horizon and the locked staleness limit. |
| Regime attribution | HMM posterior availability now includes every observation in its filtering history, not just the current rolling window. |
| Numerical timing | Evaluation requires explicit timezone-aware execution opens and decisions before the next open. Untimed legacy evidence is not silently upgraded. |
| Labels | Forward outcomes are bound to the exact source panel and validated against their declared fixed-horizon endpoints. |
| Feature mathematics | Singleton feature builders reject cross-sectional/time-series operations rather than emit misleading constants. Fractional winsorization and the legacy Gaussian variance normalizer are corrected. |
| Feature provenance | Bound feature manifests and original observations travel with snapshots; changed exports and contradictions between row availability and source extraction/mapping clocks are rejected, including when all hashes are recomputed. Reports distinguish retrospective replay and unknown provider declarations. |
| Registry and trials | Registration requires PROPOSED status. Whole batches reject duplicate/existing IDs before publication. Defensive copies prevent external mutation of stored records. Trials cannot finish twice or switch identity. |
| Tool spending | A trusted maximum charge is reserved atomically before a tool runs. Uncertain failures retain the reservation; an adapter exceeding its declared bound blocks that job. |
| Agent budgets | Counts and costs reject invalid/non-finite values; actual returned candidate counts cannot bypass limits. CLI timeout uses the smaller job/spec limit; the generic orchestrator measures worker runtime before publication. |
| Trade execution queue | Eligible orders are selected independently of the latest-200 display history, oldest first. Expired/obsolete proposals, exhausted book budgets and unarmed live books cannot fill the actionable batch. |
| Approval | The console states that authorization permits automatic submission, including after resume. Live authorization requires confirmation before the action; cancel is side-effect free. |

## Migration before resuming research

1. Preserve old artifacts. They remain historical evidence of the old engine,
   not corrected performance estimates. Rerun affected backtests and research.
2. New snapshots require `open_at` alongside `session` and `decision_at` in
   `sessions.parquet`. Supply actual exchange-calendar timestamps, including
   holidays and short sessions; do not infer production opens from weekdays.
   Old snapshot directories are immutable. Reimport into a new snapshot.
3. For materialized features, retain the build's artifact store and bind field
   names to their returned manifest hashes during import:

   ```text
   import-snapshot ... --feature-manifests field-manifests.json --feature-artifacts var/feature-artifacts
   ```

   `field-manifests.json` maps field names to manifest hashes. Original feature
   rows must match after normalizing row order and Parquet serialization.
   Unbound fields have unknown/provider-declared clocks, not verified as-run
   provenance. A hash proves consistency, not truth or data licensing.
4. Create new paper books after the engine change. Existing books retain their
   engine-hash guard; do not bypass it to append a different engine's results.
5. Custom tool registrations must supply `max_cost_usd`, including zero for an
   audited free adapter. It must be a provider-enforced upper bound, not a guess.
6. CLI-agent launches now default to denied because this runner cannot enforce
   provider token/dollar caps. `allow_unmetered_provider: true` or the documented
   CLI flag is an explicit acceptance of that risk—not evidence of a hard cap.
   Existing immutable campaigns need a new version to change this setting.

## Boundaries that remain

- Live trading remains off by default. Account-wide exposure/margin controls,
  broker reconciliation, cancellation recovery and account-specific acceptance
  testing are still required before real-money use.
- Tool-router reservations are in-memory. They are not a durable cross-restart
  spending control. Provider-enforced limits or a trusted billing gateway remain
  necessary for a hard model-spending guarantee.
- Daily OHLC bars cannot establish all intraday ordering or exact auction fills.
  Session-level strategies need appropriate timestamped market data.
- Independent data licensing/provenance approval and sealed-test discipline
  remain required. Passing unit tests does not validate an alpha.

## Verification

Final settled-patch result: **761 Python tests passed** in 66.16 seconds, with
362 dependency deprecation warnings and no failed/skipped tests. The web's
**3 authorization tests passed** separately.

Ordered verification gates:

1. `git diff --check`, `python -m compileall -q src tests`, and
   `ruff check --select E9,F63,F7,F82 src tests`: passed. This targeted lint gate
   is not a claim that every pre-existing style warning was resolved.
2. Original triggers and alternate cases: targeted regression suites passed;
   denied charging tools execute zero times, duplicate batches publish nothing,
   eligible orders survive display/budget starvation, and contradictory replay
   clocks are rejected despite recomputed hashes. Legitimate controls pass.
3. `.venv/bin/python -m pytest -q --junitxml=var/verification/money-safety-2026-09-05.xml`:
   761 passed. From `web/`, `node --test tests/execution-authorization.test.mjs`,
   `tsc --noEmit`, and `npm run build`: passed.

Source changes cover `ledger.py`, `tools.py`, `contracts.py`, `orchestration.py`,
`subagents.py`, `cli_agents.py`, `campaign_workers.py`, `cli.py`, `portfolio.py`,
`book_regimes.py`, `strategy_builder.py`, `panel.py`, `snapshots.py`, `numerical.py`,
`prediction_artifacts.py`, `pipeline.py`, `dsl.py`, `models.py`, `features.py`,
`alternative_data.py`, and the web approval page/helper. Corresponding tests,
launcher/config examples and onboarding documents are updated. Synced reference
files are unchanged. Verification was completed before committing or pushing
the remediation changes.

The security-fix workflow used independent boundary investigation and one
independent patch-review cycle, split across execution/budget and numerical
changes. The follow-up findings were reproduced and corrected: exhausted-book
starvation, underreported generic-worker runtime, duplicate-ID partial
publication, replay relabeling, and scalar missingness suppression.

Primary regression suites are `test_budget_controls.py`, `test_money_safety.py`,
`test_trade_control.py`, `test_snapshot_binding.py`,
`test_research_math_regressions.py`, `test_portfolio_integrity.py`,
`test_book_regimes.py`, and the existing numerical, pipeline and strategy suites.
They retain legitimate controls: free/within-budget tools, normal lifecycle
promotion, bounded CLI fixtures, ordinary shadow approvals, valid source clocks,
reserialized feature exports and normal portfolio fills.

The web build and TypeScript checks pass. Three executable authorization-unit
tests cover live cancel, confirmation-before-action and ordinary shadow approval.
The local browser rendered the corrected warning and button; automated interaction
stalled at the native confirmation dialog. Native-dialog end-to-end verification
therefore remains incomplete. The isolated fixture server was stopped and had
no broker imports or account connection. It can be rerun with
`python tests/manual_approval_fixture.py` after building the web console.

The earlier security workbench report could not be sealed because its final
draft was rejected; this document does not represent a successful workbench
scan completion. A separate fix report records the code remediation.

No dependency upgrade was attempted. Existing `hmmlearn`/NumPy deprecation
warnings remain. One early worker pipeline test failure had no captured cause;
subsequent focused and full-suite runs passed. The assertion now includes
failure details; no timing or safety assertion was removed to hide it.
