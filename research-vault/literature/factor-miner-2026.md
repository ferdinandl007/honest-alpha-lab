---
paper_id: factor-miner-2026
year: 2026
reviewed_on: 2026-09-04
lane: research_memory
read_depth: methods_and_experimental_setup
replication_status: not_run
code_status: runnable_implementation_not_verified
---

# FactorMiner: A Self-Evolving Agent with Skills and Experience Memory for Financial Alpha Discovery

Source: [primary source](https://arxiv.org/html/2602.14670v1).

## Evidence reviewed

A retrieve–generate–evaluate–distill loop turns previous experiments into reusable success patterns and failure constraints. The reviewed experiments use 10-minute data, 2024 training and 2025 testing, including Chinese equities and crypto.

## Application to Honest Alpha Lab — our recommendation

Store concise, evidence-linked failure lessons: invalid availability, unstable sign, turnover, duplicate exposure and unavailable inputs. Retrieve them before the next proposal.

## Limits and remaining checks

Intraday results are not evidence for our daily 5/10/20/60-day targets. This review did not verify a runnable public implementation. A factor library or reported absolute IC is not equivalent to reproducible, sign-fixed net returns.

## Connections

- [[literature/alphaprobe-2026]]
- [[literature/rd-agent-quant-2025]]
- [[literature/index]]

