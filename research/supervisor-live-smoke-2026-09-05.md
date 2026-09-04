# Real researcher smoke test — 5 September 2026

Outcome: one actual Codex CLI researcher was scheduled through the persistent
supervisor, executed collection/parsing code in its network-enabled research
workspace, and completed a discovery result. This was not a backtest or alpha
benchmark. No perpetual service was left running by this smoke test.

## Independently checked artifacts

- Official source: [NOAA CPC Niño 3.4 table](https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/ensostuff/detrend.nino34.ascii.txt).
- Retrieved bytes: **30,328**; independently recomputed SHA-256:
  `be3b8c966b63b9c4527171b92ad425e9582914a14a4d62520ed3dc7a3ef35006`.
- Independently counted **918** five-field monthly rows. The final row in this
  retrieved file is June 2026; this is not a claim of real-time data freshness.
- Recorded collection completed at `2026-09-04T22:41:46.588269+00:00`.
- Stable supervisor job: `7b1f3503-ef96-5200-a9e6-41e63eb70cdf`, one attempt,
  terminal `completed` state. CLI elapsed time was approximately 91 seconds.

The local research directory contains raw bytes, provenance, an executed
collector/parser, a parsed summary and the hypothesis note. The local completed
handoff contains findings and explicitly no accepted alpha candidates.
Downloaded data, per-run logs and workspaces under `var/` are intentionally
excluded from the public repository; the public report is not a redistributed
raw-data evidence bundle. To collect your own evidence, run
`scripts/run_supervisor_smoke.py --output-root var/my-smoke-test` with the project
Python environment and configured CLI authentication.

## Hypothesis, not a verified signal

The researcher suggested studying whether ENSO anomalies lead regional heating
demand and gas-distribution utility outcomes. It identified unresolved dated
service-territory mappings, ownership, customer mix and tariff/weather-normalization
effects. Current revised climate observations do not establish historical
publication vintages. No stock returns were tested and no return claim is made.

Direct collection worked without forcing the agent through a catalog or the
optional constrained collector. One initial shell operation failed; the agent
changed its implementation and retained the failure in its provenance notes.

## Spending-accounting finding

The agent's final response reported `agent_tokens: 0`. Its provider-generated
`turn.completed` log instead records **109,153 input tokens** (including **85,120
cached input tokens**) and **2,100 output tokens**. Cached input is a subset, not
an additional amount to sum. This does not establish a dollar cost.

The declared job allowance of 15,000 tokens was not a hard enforcement mechanism.
This live test demonstrates why self-reported usage cannot control an always-on
service. Provider-event accounting and pre-/during-execution global spending
enforcement remain required. The smoke job was capped to one invocation and was
not left in a recurring service process.

## Software checkpoint

Full suite: **496 passed**, zero failures/errors/skips, five existing upstream HMM
deprecation warnings. Machine-readable evidence:
`var/verification/supervisor-2026-09-05.xml`. This covers the tested scheduling and
worker behavior, not multi-day operational reliability or market profitability.
