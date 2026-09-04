# Diversity loop and research-memory vault

`DiversityResearchLoop` conducts a round of distinct, budgeted research tasks. Before a task is created it adds `ResearchVault.shared_context()` to the immutable context: prior candidate specifications, data lineages, evaluation reward/similarity summaries, and a direct instruction to find a different mechanism rather than restating a known formula.

After the round, `ResearchVault` writes append-only Markdown and JSON companions under `research-vault/`:

- `candidates/<id>.md`: a proposed signal, its immutable specification, and Obsidian wiki-links to its round, agent kind, and datasets;
- `datasets/<id>.md`: a dataset node to which Obsidian automatically adds candidate backlinks;
- `rounds/<id>.md`: exact task/run trace and its candidate links;
- `evaluations/<candidate>-<hash>.json`: immutable numerical/reward evidence;
- `validation-plans/<candidate>-<hash>.md`: fixed robustness rubric before any result is seen;
- `index.md`: regenerated navigation only.

The vault is intentionally a *memory and audit surface*, not an approval mechanism. It stores failed and low-reward ideas as well as promising ones, so future agents can diversify away from prior attempts. Measured correlations/similarity may enter the vault only through `record_evaluation()` after the numerical service has produced an `EvaluationResult` and `RewardResult`.
