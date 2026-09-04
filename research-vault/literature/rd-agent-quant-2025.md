---
paper_id: rd-agent-quant-2025
year: 2025
reviewed_on: 2026-09-04
lane: orchestration
read_depth: methods_and_experimental_setup_and_repository
replication_status: not_run
code_status: repository_inspected_not_executed
---

# R&D-Agent-Quant: A Multi-Agent Framework for Data-Centric Factors and Model Joint Optimization

Source: [primary source](https://arxiv.org/html/2505.15155v2) · [author repository](https://github.com/microsoft/RD-Agent).

## Evidence reviewed

Uses iterative specification, implementation, validation and analysis, with experience retrieval and a contextual search scheduler. The main reviewed CSI300 experiment trains on 2008–2014, validates on 2015–2016 and tests January 2017–August 2020.

## Application to Honest Alpha Lab — our recommendation

First integration spike: adapt the quant workflow and failure-memory interfaces instead of rebuilding them. The repository displays an MIT license; pin a commit and check dependency/data rights separately.

## Limits and remaining checks

Factor/model joint optimization changes multiple variables. Our first comparison should freeze the conventional model. Chinese-equity results do not establish US transfer; current repository features beyond quant research are not automatically in scope.

## Connections

- [[literature/qlib-2020]]
- [[literature/factor-miner-2026]]
- [[literature/alphabench-2026]]
- [[literature/index]]

