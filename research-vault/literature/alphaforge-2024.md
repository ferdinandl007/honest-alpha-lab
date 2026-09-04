---
paper_id: alphaforge-2024
year: 2024
reviewed_on: 2026-09-04
lane: symbolic_combination
read_depth: methods_and_experimental_setup
replication_status: not_run
code_status: not_verified
---

# AlphaForge: A Framework to Mine and Dynamically Combine Formulaic Alpha Factors

Source: [primary source](https://arxiv.org/html/2406.18394v1).

## Evidence reviewed

Separates factor generation from dynamic combination. The reviewed setup retrains annually: the first split uses 2010–2016 training, 2017 validation and 2018 testing; subsequent splits extend through a 2022 test.

## Application to Honest Alpha Lab — our recommendation

Preserve separate discovery and conventional combination services. Test equal weighting before adding train-only ridge or LightGBM combination.

## Limits and remaining checks

A dynamic combiner can create apparent discovery gains. Hold the combiner fixed when comparing generators; do not transfer reported results to our market or treat a changing historical model as forward evidence.

## Connections

- [[literature/alphagen-2023]]
- [[literature/rd-agent-quant-2025]]
- [[literature/index]]

