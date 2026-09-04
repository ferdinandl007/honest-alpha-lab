# Reward and benchmark harness

`ResearchRewardHarness` scores completed *numerical-service* evaluation records. It is not a synthetic benchmark generator and it has no method that changes Alpha Registry state.

For every candidate, record `RewardEvidence` after purged walk-forward. The evidence ties the reward to the candidate, immutable snapshot and policy hashes in `EvaluationResult`, its raw p-value and global trial count, PIT and lineage verification, turnover, library similarity, data cost, capacity proxy, complexity, and optional regime-conditional ICs. Alternative-data candidates also require an independently verified proxy → fundamental → earnings → return chain.

The reward combines out-of-sample and incremental rank IC, fold persistence, and regime contribution, then deducts similarity, turnover, data cost, complexity, and a trial-count diagnostic. Separately, hard gates require PIT/lineage verification, persistent positive folds, IC and stability thresholds, Bonferroni-adjusted p-value, similarity, turnover, capacity, and the alternative-data semantic chain. Passing those research gates only yields `eligible_for_registry_validation`; a sealed result is additionally required for `eligible_for_registry_acceptance`. The numerical validator remains the only actor allowed to promote the candidate.

Use `leaderboard()` to compare real completed trials under one immutable policy. It sorts eligible candidates first, but preserves all failure gates and components so a weak high-reward candidate cannot be silently treated as accepted. The starting thresholds are in [the example policy](../config/research-reward-policy.example.json) and should be locked before a research round.
