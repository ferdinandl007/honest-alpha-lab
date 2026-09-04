# Contributing to Honest Alpha Lab

The goal is a continuously operating research system that produces trustworthy
evidence. A compelling hypothesis is welcome. An unsupported performance claim
is not a result.

## Start here

1. Read [the onboarding guide](docs/onboarding.md) and
   [implementation audit](docs/implementation-status.md).
2. Open an issue describing the mechanism, engineering gap or reproducible bug.
3. Keep changes focused; include tests and explain what the tests establish.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[analytics,models,storage,dev]'
python -m pytest -q
```

PostgreSQL integration tests need local `initdb`, `pg_ctl` and `postgres`
binaries. Set `HAL_TEST_PG_BIN` to their directory and run as a non-root user.
The tests create disposable private clusters; never point them at production data.
Some process-group tests are POSIX-specific. Inspect skipped tests before making
a full-suite verification claim.

## Useful contributions

- Provider-based usage accounting and enforceable campaign spending limits.
- Deployment, health checks, recovery and long-running reliability tests.
- Point-in-time data adapters, entity mappings and independently checked vintages.
- Better research planning, failure retrieval and library-level novelty measures.
- Real-data, matched-budget search benchmarks with reproducible negative results.

## Keep the evidence honest

Distinguish software fixtures, retrospective experiments, forward observations
and independently validated signals. Record failed trials. Keep final-test
outcomes out of research feedback. Do not replace missing financial observations
with invented data or remove hard cases merely to improve a backtest.

Do not commit credentials, downloaded datasets without redistribution rights,
personal data, local task logs or private research-vault records. Public releases
exclude `sources/`, `var/`, local databases and per-run vault contents.

Phase 1 has no live orders, brokerage integration, LLM fine-tuning or agent
self-approval. Discuss changes to those boundaries before implementing them.
