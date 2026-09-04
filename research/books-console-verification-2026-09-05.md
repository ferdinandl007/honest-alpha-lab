# Strategy, session-book and private-console verification

Local checkpoint: 5 September 2026. This is software verification, not a financial
benchmark. No newly validated alpha or profitable trading strategy is claimed.

## Verified

- Complete Python suite: **658 passed** in 61.06 seconds.
- 362 upstream hmmlearn/NumPy deprecation warnings; no failing tests.
- Machine-readable local evidence: `var/verification/books-console-2026-09-05.xml`.
- Focused Ruff checks pass for new engines, API/control code and protocol tests.
- Frontend TypeScript check and static production build pass on Node 22.23.1.
- Built static page plus authenticated status API passed a local HTTP smoke check
  against a temporary empty, halted book store; the temporary service was stopped.
- Strategy compiler tests cover frozen policy, causal combinations, numerical
  training returns and fake-CLI agent integration.
- Paper ledger tests cover append-only inputs, idempotency, restart/replay and
  forward-timing checks.
- Session tests cover exact timing, weekends/early closes, missing bars, costs,
  stops/targets, independent books and unfinished holdings.
- State-report tests cover prior-session conditioning and future-data isolation.
- Actual local HTTP tests cover separate proposer/operator authority and origin
  checks. Broker tests use fake transports only, including fresh/stale quotes
  and one-attempt live routing. The signature matches Webull's public test vector.

## Explicit gaps

- No personal Webull connection, token flow, order or market-data entitlement
  was tested. No real orders were placed. Live arming remains unset by default.
- No Tailscale or public deployment was performed. The console is built for the
  private Python service, not a broker-connected preview.
- No browser click/screenshot or supported WebMCP execution test was performed;
  the optional read-only page tool is implemented but unverified in a browser.
- The strategy agent was tested with a labeled fake CLI in this checkpoint,
  not represented as an actual alpha-mining run.
- Live session scheduling, auction orders, normalized feed automation,
  account-wide netting/risk, and automated broker reconciliation remain open.
- Frontend dependencies were updated to resolve high-severity audit findings.
  Two moderate findings remain in the shadcn tooling chain (`qs`/`body-parser`).
  The registry's latest `qs` was 6.15.3 and `npm audit fix` did not resolve them.
  These are not used by the Python static/API server; do not expose the development
  tooling server. This is not a complete dependency or security certification.

See [the operational guide](../docs/books-and-control-room.md) for exact schemas,
mode meanings, setup steps, provider sources and rollout prerequisites.
