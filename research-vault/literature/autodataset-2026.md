---
paper_id: autodataset-2026
year: 2026
reviewed_on: 2026-09-04
lane: dataset_discovery
read_depth: pipeline_and_case_study_and_repository
replication_status: not_run
code_status: repository_inspected_not_executed
---

# AutoDataset: A Lightweight System for Continuous Dataset Discovery and Search

Source: [primary source](https://arxiv.org/html/2603.07271v1) · [author repository](https://github.com/EIT-NLP/AutoDataset).

## Evidence reviewed

Discovers dataset releases from papers using staged detection, description/link extraction and semantic retrieval. The paper links to a public implementation.

## Application to Honest Alpha Lab — our recommendation

Borrow the paper-to-dataset record pipeline for our source scout. Use prompted extraction or existing permitted components in phase 1; do not train an LLM. Extend discovery to official agency and provider documentation.

## Limits and remaining checks

This is general dataset discovery, not evidence of financial alpha. A found URL does not establish research rights, vintages, asset identity, economic relevance or usable coverage. The trained components in the original design need not be copied.

## Connections

- [[literature/satellite-information-risk]]
- [[literature/rd-agent-quant-2025]]
- [[literature/index]]

