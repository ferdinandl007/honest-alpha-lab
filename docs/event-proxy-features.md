# Open exploration, evidenced feature handoff

Researchers can choose hypotheses, follow new sources, prototype code and revise
their approach. They are not confined to a catalog. Collection and experimental
execution require actual supplied tools, lawful access, budgets and isolated
workspaces. Version-2 discovery prompts make this distinction explicit; prompts
alone do not grant capabilities or enforce isolation.

`features.py` is the structured handoff, not a required discovery workflow. It
turns extracted event scores or economic measurements into exact-session,
stock-level Parquet observations consumable by the numerical snapshot/DSL path.
It accepts no arbitrary generated code. Researchers may use custom experimental
code to produce its inputs; review and admission remain separate.

## Executable interfaces

Use `materialize_features(plan, sources, exposures, sessions, artifacts)` from
Python, `EvidenceFeatureBuildTool` registered as `POINT_IN_TIME_FEATURE_BUILD`,
or the file interface:

```sh
.venv/bin/python -m honest_alpha_lab build-features \
  --request feature-request.json \
  --source-artifacts var/source-artifacts \
  --output-artifacts var/feature-artifacts
```

The request has exactly four keys: `plan`, `sources`, `exposures`, `sessions`.
Their fields are the frozen contracts `FeaturePlan`, `SourceVintage`,
`ExposureVintage`, `FeatureSession`. Dates and aware timestamps use ISO format.
Source spans contain `start`, `end`, `quote`; offsets address characters in exact
UTF-8 text, not bytes or a subsequently cleaned document. Archive that text plus
publication and mapping evidence using `LocalArtifactStore.put`. Source references
are hashes, not agent-selected paths.

The command returns hashes for an immutable Parquet file and its manifest. The
manifest includes input records, policy and per-row source/mapping contributions.
Every output says `independently_approved: false` and
`financial_alpha_verified: false`. Empty builds retain an empty table schema.

## Timing and aggregation contract

| Rule | Behavior |
| --- | --- |
| Default `as_run` clock | Eligible only after publication, collection and extraction; timestamps must be ordered. |
| `publication_replay` | Separate retrospective experiment; modern LLM extractions require explicit opt-in. Never evidence of historical execution. |
| Trading-session lag | First decision at/after the clock plus the declared number of supplied exchange sessions. Truncated warm-up calendars are rejected. |
| Revisions | Latest eligible publication for an observation; earlier rows retain earlier values. Withdrawal suppresses an observation rather than reviving its older value. |
| `event_sum` | Sum distinct active events weighted by exposure effective on each event's observed date and known at the decision. Optional exponential decay. |
| `latest_proxy` | Latest observed period per source, not latest publication across periods. Sum against decision-date exposures; require every active mapped source. |
| Expiry | Inclusive maximum age in **calendar days** from observation; revisions do not reset freshness. |
| Mapping revisions | Select latest known effective version before testing termination. Zero weight removes exposure. No weight normalization. |
| Missing data | No event is missing, not zero. Missing/stale/withdrawn proxy components suppress the aggregate. Observed zeros remain valid. |
| Output clock | Each aggregate is available at its session decision, conservatively including daily aggregation. |

Supply one dataset/measurement definition per build. Stable observation IDs
identify revisions. Proxy records cannot have multiple identities for the same
source and observed period. Mapping records represent one relationship per
asset/source/effective date/vintage; separately evidenced subrelationships must
be combined upstream rather than silently double-counted.

The supplied calendar determines trading sessions; the compiler does not invent
exchange holidays. The historical universe is enforced downstream by
`ParquetSnapshot`. After independent review, an operator may combine the resulting
long observations with market observations into a **new** snapshot. Never overwrite
an immutable snapshot or assume its rights declaration covers a new source.

## What is not proved

Hashes and matching quotations establish byte identity and traceability, not
truthful vendor timestamps, lawful rights, correct extracted semantics or a valid
economic asset relationship. Independent source/extractor/map review remains
required. Extractor IDs and publication-evidence files are not authenticated
attestations. Automated source discovery, safe collection and isolated arbitrary
research-code execution are not implemented by this compiler.

The older `AlternativeDatasetCreationAgent.build_feature` and
`PointInTimeAssetMapper` remain reference interfaces, not substitutes for this
version-aware session materializer. Their actor-name checks are not production
authentication. Legacy economic-hypothesis contracts still require an earnings
pathway; generalization to liquidity/flow/risk-premium mechanisms is outstanding.

Tests exercise clocks, revisions, expiry, mapping, missingness, evidence tampering
and actual CLI → Parquet → snapshot → DSL execution. They are software correctness
fixtures, not historical financial benchmarks or discovered alphas.
