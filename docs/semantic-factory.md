# Semantic alpha factory

> Implementation note, 5 September 2026: the catalog-only flow below describes
> the legacy reference adapter, not the desired limit on researchers. Version-2
> discovery prompts permit open-ended source exploration and permitted collection
> with supplied tools. See [the executable event/proxy handoff](event-proxy-features.md).
> The legacy earnings-only contracts and actor-name approval checks are not a
> complete mechanism framework or authenticated production approval boundary.

The semantic factory is the part of Honest Alpha Lab that looks for leading economic information rather than recombinations of financial fields. It is designed for hypotheses such as “hub aircraft movements may lead package revenue” or “storm exposure may lead home-improvement demand.” It does not treat a creative story as alpha.

## Required evidence chain

```text
Observable phenomenon
  -> economic mechanism
  -> fundamental target
  -> earnings-surprise prediction
  -> post-announcement abnormal-return prediction
  -> incremental contribution to the alpha library
```

`EconomicHypothesis` requires every link before catalog discovery begins. `SemanticEvidence` retains the three out-of-sample information measures, similarity, cost, complexity, and instability. `SemanticRewardPolicy` implements the stated research objective: incremental information minus similarity, cost, complexity, and instability.

## Dataset approval and feature creation

1. `AlternativeDatasetCreationAgent.discover` searches only a supplied `DatasetCatalog` using the hypothesis’s discovery terms.
2. The agent can register only discovered candidates. It cannot approve a license or create usable lineage.
3. `DatasetRegistry.approve` requires the independent `data-steward` actor and rejects data lacking verified research and derived-feature rights, point-in-time semantics, or a non-personal-data guarantee.
4. `AlternativeDatasetCreationAgent.build_feature` accepts only an approved candidate and produces `PITObservation` values with the source publication time plus an explicit availability lag.
5. `alpha_hypothesis` hands the approved dataset and feature definition to `AlternativeDataAgent`, which can propose an alpha to the shared registry. Numerical validation remains a separate step.

The source data reader belongs behind a catalog/sample adapter. The reference package does not scrape websites, accept terms, buy data, or handle personal data.

## Subagent contract

Use `AgentTask.new(job, prompt_name, context)` and submit it to `SubagentOrchestrator`. Jobs are immutable and have snapshot, prompt, trial, runtime, token, and data-cost limits. Every subagent start, completion, failure, and allowlisted tool call is hash-chained.

For an external provider, construct `DelegatingSubagentWorker(agent_kind, dispatcher)`. The dispatcher shape is:

```python
def dispatcher(rendered_prompt: str, envelope: dict[str, object]) -> SubagentResult:
    ...
```

The envelope contains task/job identifiers, the immutable snapshot hash, and allowed tool names. It has no sealed-test or registry-promotion capability. Tools are executed by `ToolRouter`, which checks both agent permission and the registered job’s data-cost budget.

## Example scenario

For a FedEx research job, the semantic agent can formulate a “cargo hub aircraft movements” hypothesis and search a catalog with `faa`, `cargo`, and `fedex`. A data steward must approve the returned dataset before daily hub counts become a point-in-time feature. The resulting feature must first demonstrate a lead relationship to revenue, then earnings surprise, then abnormal returns. Only then is it a candidate for the numerical validation workflow.
