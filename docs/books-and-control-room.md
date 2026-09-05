# From signals to books

This is an execution research preview, not a certified trading system. Live
execution is off by default. No real orders were placed during implementation.
The original phase-1 no-broker scope has been extended at the owner's request;
historical mission documents remain unchanged as an audit trail.

## What is connected

| Path | Implemented behavior | Boundary |
| --- | --- | --- |
| Strategy agent | CLI agent selects signal combinations; trusted code compiles and backtests them | Development data only; no admission or order authority |
| Daily portfolio | Equal-weight, inverse-volatility, minimum-variance comparisons | Allocation trained before internal test; not a sealed final test |
| Session books | Buy before close, exit next open or a configured offset | Historical intraday replay, not a live session scheduler |
| Paper ledger | Persistent daily historical replay or forward shadow ingestion | Caller supplies normalized signals and completed daily bars |
| Control room | Book policies, trade inbox, approve/reject, submit, halt | Private single-operator service; not public multi-user hosting |
| Webull HK | Separate sandbox, read-only data and explicitly armed production routes | US whole-share limit orders only; account connection not yet tested |

These are deliberately separate interfaces. A session backtest does not silently
start a broker strategy. A shadow intent is not a simulated fill. A broker HTTP
response is not proof of acceptance or execution.

## 1. Build daily strategies from a signal library

Run from the repository root with the Python environment activated:

```sh
python -m honest_alpha_lab build-strategies --request strategy-context.json --output-directory var/strategies
```

The request is the locked research context, not the agent's proposed strategy:

- `portfolio_request`: `bars_csv`, `scope: "development"`, `development_start`,
  `development_end`, `test_start`, `test_end`, explicit `execution_policy`;
  optional `initial_cash` and `allocation_policy`.
- `bars_sha256`: exact input file digest.
- `signal_catalog`: map each permitted signal ID to `{csv_path, sha256}`.
  Signal CSV columns: `signal_id,asset,score,available_at,snapshot_hash`.
- `allowed_horizons`, `allowed_sides`, `allowed_rebalance_days`: operator choices.
- `signal_lag_days`, `max_staleness_days`: explicit causal timing limits.

The agent produces blueprints with `strategy_id`, `name`, `signal_ids`,
`combination` (`linear_equal` or `rank_equal`), `horizon_days`, `side`,
`rebalance_days`, `execution: "next_bar_limit"`, and `rationale`. It cannot invent
input paths, loosen costs, tune the sealed test, or supply its own training P&L.
The compiler derives development sleeve returns with the numerical backtester,
then runs conventional allocation comparisons on the later internal test.

The default uses the existing Codex CLI adapter. Other CLI providers can be passed
through the Python `run_strategy_agent(..., worker=..., budget=...)` interface.
Timeouts and declared budgets apply; provider-enforced spending is still a gap.
This path's automated tests use a labeled fake CLI, not claimed mined alpha.

See [the daily portfolio input guide](state-and-portfolio-workflow.md) for price,
execution-cost and allocation schemas.

## 2. Buy before close, sell next open

Timing is relative to the exchange session, not the computer's local clock. Each
book has its own asset, exchange, capital, leverage, side, stop/target and offsets.
For example, a book entry in a session request is:

```json
{
  "book_name": "overnight-us",
  "asset": "AAPL",
  "exchange": "XNYS",
  "entry_minutes_before_close": 5,
  "exit_minutes_after_next_open": 0,
  "side": "long",
  "starting_cash": 100000,
  "max_capital": 25000,
  "leverage": 1,
  "cost_bps": 5,
  "stop_loss_pct": 0.02,
  "take_profit_pct": 0.04
}
```

This is a configuration illustration, not a recommendation or a performance result.
Use the correct exchange/calendar for the selected instrument. The full request
contains `books: [...]`, `calendar_csv`, `bars_csv`, both files' lowercase SHA-256
digests as `calendar_sha256` and `bars_sha256`, and timezone-aware `rules_set_at`,
`start_at`, `end_at`. Rules must be fixed before the requested replay start;
the supplied timestamp alone does not prove genuine historical pre-registration.

```sh
python -m honest_alpha_lab session-backtest --request session-request.json --output-directory var/session-reports
```

Input contracts:

- Calendar: `exchange,session,open_at,close_at`. Include actual sessions with
  timezone offsets, holidays omitted, DST and early closes already resolved.
- Intraday bars: `exchange,asset,open_at,end_at,open,high,low,close,kind`.
  Ordinary bars represent `[open_at,end_at)` intervals. They must cover held
  regular-session intervals without gaps. Timed fills use the exact bar **open**.
- Entry exactly at close requires an explicit `kind=auction` point record at
  that close, with identical OHLC values. Daily candles cannot substitute.

Each book has separate cash and holdings. Missing required data fails the book,
not a favorable guessed fill. Weekend gaps, early closes, stops/targets, entry and
exit costs, and an unfinished final holding are handled explicitly. If stop and
target both hit inside an observed bar, stop wins. Overnight gaps fill at the
observed open; a stop does not guarantee its trigger price.

Limitations: fractional simulation, assumed short borrow availability, no borrow
or financing fees in this session engine, no intrabar margin calls or after-hours
stop monitoring. Independent research books do not imply segregated broker
subaccounts. Other timing families require extending the typed session engine;
this is not an unrestricted strategy scripting language.

## 3. Historical and forward paper ledgers

```sh
python -m honest_alpha_lab paper-book --request paper-request.json --output-directory var/paper-receipts
```

Every request has `action` (`create`, `ingest`, `status`), `db_path` and `book_id`.
Create adds `config`; ingest adds `event`. Configuration is immutable: create a
new book ID for changed strategy rules. Events are append-only and idempotent by
ID and content. Restarting preserves the ledger.

Configuration has `mode: "historical_replay"` or `"forward_shadow"`,
`initial_cash`, `strategies` (mapping IDs to daily strategy definitions), fixed
`allocation` (mapping those IDs to weights), and explicit `execution_policy`.
Forward mode additionally requires `max_signal_age_seconds` and
`max_bar_delay_seconds`.

Signal events contain `event_id`, `kind: "signals"`, and `signals`, each with
`strategy_id,asset,score,available_at,snapshot_hash`. Bar events contain `event_id`,
`kind: "bars"`, `window_start`, `window_end` and `bars`, each with
`day,asset,open,high,low,close,dollar_volume,borrow_available`. A window is a complete
UTC midnight-to-next-midnight day. The caller supplies the actual session calendar.
Forward signals must arrive before execution windows; old backfills cannot be
presented as live paper observations.

Status includes cash, NAV, positions, fills, costs and drawdown. Daily history is
replayed deterministically on ingestion, so compute grows with book history.
This is a daily shadow engine; it is not yet the live intraday session engine.
Webull raw history collection does not automatically normalize or feed this ledger.

## 4. Market-state diagnostics

Add `market_history_csv` to a daily portfolio workflow request to get both HMM
and Gaussian-mixture book breakdowns. CSV columns are `day,close,available_at` for
one consistent market index, with unique ordered dates and aware publication
times. Its content hash becomes part of the report inputs. If supplying
`input_hashes`, include the `market_history` digest alongside the existing inputs.

State models train on development dates only. A return is paired with the prior
supplied session's filtered probabilities, known before the return day starts in
UTC. Reports include weighted return, volatility, loss probability, effective
sample size and missing dates. Insufficient data and nonconvergence are explicit.
These are descriptive diagnostics, not proof that a strategy works in every
regime. State numbers are method-local. Session-engine reports do not yet attach
this daily portfolio breakdown automatically.

## 5. Private approval website

The Sites-scaffolded frontend exports static files; the Python service owns all
private state and broker access. Node 22.13+ is needed only for the frontend build.

```sh
cd web
npm ci
npm run build
cd ..
python -m honest_alpha_lab serve-console --port 8787
```

Before starting, configure `HAL_CONSOLE_TOKEN` with a privately generated random
token of at least 32 characters. Optionally configure a **different**
`HAL_PROPOSAL_TOKEN` for researchers. Generate these in a password manager or
secret manager; do not commit them, put them in URLs, or pass operator authority
to agent jobs. The browser holds the operator token in memory, not local storage.

Open `http://127.0.0.1:8787`. Connect, create a **shadow** execution book, and keep
approval required initially. Proposals carry immutable IDs, symbol/side/quantity,
limit and reference prices, quote timestamp, book and rationale. **Approval
authorizes execution**: a running executor may submit an approved intent without
a separate Send click, including after a halt is lifted. The live approval button
asks for confirmation before granting that authority. Direct operator API clients
must treat `approve: true` identically. Policy changes invalidate outstanding intents.
Quotes must be no more than 60 seconds old when proposed and dispatched; stale
intents require a fresh proposal and approval. A halt is enabled initially.

API: operator `GET /api/status`, `GET /api/paper-status?book=...`; operator POST
`/api/books`, `/api/review`, `/api/dispatch`, `/api/halt`; either token can POST
`/api/proposals`. Only the proposer token belongs in a research workspace.
The optional page-agent tool is read-only and cannot approve or send trades.

For private remote access, install and authenticate Tailscale on the host, set
tailnet access rules, then use [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve):

```sh
tailscale serve --bg http://127.0.0.1:8787
```

Restart the Python server with `--origin https://YOUR-EXACT-TAILNET-HOST` matching
the HTTPS origin Tailscale reports. Keep the service on loopback. Do not substitute
public Funnel. A remote deployment needs persistent private storage, process
supervision, backups and TLS/access controls; no deployment was performed here.
This small server is not a hardened public multi-user trading service.

## 6. Webull sandbox to live

The adapter uses the [official Webull HK environments](https://developer.webull.hk/apis/docs/sdk/)
and [request-signing protocol](https://developer.webull.hk/apis/docs/authentication/signature/).
Keys remain server-side. Use your own approved application and token; complete
Webull's required [production authentication](https://developer.webull.hk/apis/docs/authentication/token/).
Sandbox data are restricted to AAPL; broader shadow research needs another feed.

| Book mode | Destination | Credential prefix |
| --- | --- | --- |
| `shadow` | Local intent journal only | None |
| `paper` | `api.sandbox.webull.hk` | `HAL_WEBULL_SANDBOX_` |
| `live` | `api.webull.hk` | `HAL_WEBULL_LIVE_` |

Each credential prefix needs `APP_KEY`, `APP_SECRET`, `ACCESS_TOKEN` environment
variables. Separate read-only `webull-data --mode data_only` uses
`HAL_WEBULL_DATA_`; sandbox data use the sandbox prefix. Example collector:

```sh
python -m honest_alpha_lab webull-data --mode sandbox --symbol AAPL --output-directory var/provider-data
```

Raw provider responses are archived, not certified point-in-time research panels.
[API market-data entitlements](https://developer.webull.hk/apis/docs/market-data-api/overview/)
must be checked separately from the broker app's subscriptions.

Live mode requires the operator to set deployment variable
`HAL_ENABLE_LIVE_TRADING=YES_I_ACCEPT_REAL_ORDERS` **and** select live in the book.
Turning off approval in a live book additionally requires
`HAL_ENABLE_AUTO_LIVE=YES_I_ACCEPT_UNATTENDED_ORDERS`. Neither variable is set by
this project or the web interface. Keep both unset during setup. A trusted
supervisor `execution` campaign can call `dispatch_ready()` for approved or
explicitly unattended intents, but must run separately from research credentials.

Current production orders are US equity, whole-share, regular-session DAY limit
orders through the HK endpoint. This does not implement HK enhanced-limit/BCAN
orders, opening/closing auctions, broker-native bracket stops, streaming fills,
automatic cancellations or cash/positions reconciliation. Book caps and a fresh
broker quote protect each submission, but there is no account-wide netting,
portfolio-exposure/margin/drawdown engine across books yet.

One submission attempt is reserved durably before network access. Ambiguous
outcomes remain `unknown` and are **not** retried as new orders. Query
`WebullHKClient.order_detail(account_id, client_order_id)` and reconcile with the
broker before any operator intervention; no automatic reconciliation UI exists.
Halting blocks new dispatch, not already reserved/in-flight or submitted orders.

Before real deployment: add and verify reconciliation/account-wide risk, run
account-specific sandbox acceptance tests, test recovery and cancellations, and
complete a supervised paper period on licensed real data. Live capability in
source code is not live-readiness certification. No credentials or real orders
were used to verify this implementation.
