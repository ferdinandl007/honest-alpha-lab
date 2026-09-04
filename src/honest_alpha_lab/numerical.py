"""Conventional, date-grouped development evaluation over observed panels.

This service does not approve alphas, read sealed data or report simulated profit.
Its fixtures test calculations only; financial benchmarks must use verified data.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib.metadata import version

import numpy as np

from .contracts import ContractError, canonical_hash
from .dsl import Formula
from .panel import ForwardLabels, ResearchPanel, cross_sectional_rank


@dataclass(frozen=True)
class WalkForwardConfig:
    min_train_days: int = 252
    test_days: int = 63
    embargo_days: int = 5
    min_assets: int = 20
    min_train_rows: int = 100
    min_folds: int = 2
    model: str = "ridge"
    regularization: float = 1.0
    bootstrap_samples: int = 1000
    seed: int = 0

    def __post_init__(self):
        for name in ("min_train_days", "test_days", "min_assets", "min_train_rows",
                     "min_folds", "bootstrap_samples"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ContractError(f"{name} must be a positive integer")
        if self.min_assets < 3 or self.embargo_days < 0:
            raise ContractError("need >=3 assets and a nonnegative embargo")
        if not np.isfinite(self.regularization) or self.regularization <= 0:
            raise ContractError("regularization must be finite and positive")
        if self.model not in {"ridge", "elastic_net", "lightgbm"}:
            raise ContractError("unknown conventional model")


@dataclass(frozen=True)
class FoldEvidence:
    train_start_index: int
    train_end_index: int
    last_training_label_end_index: int
    test_start_index: int
    test_end_index: int
    train_rows: int
    test_rows: int
    measured_days: int
    rank_ic: float
    baseline_rank_ic: float
    augmented_rank_ic: float
    incremental_rank_ic: float


@dataclass(frozen=True)
class DevelopmentReport:
    formula_hash: str
    snapshot_hash: str
    panel_hash: str
    labels_hash: str
    policy_hash: str
    library_hash: str
    horizon: int
    folds: tuple[FoldEvidence, ...]
    measured_date_indices: tuple[int, ...]
    daily_rank_ic: tuple[float, ...]
    daily_baseline_rank_ic: tuple[float, ...]
    daily_augmented_rank_ic: tuple[float, ...]
    daily_incremental_rank_ic: tuple[float, ...]
    rank_ic: float
    incremental_rank_ic: float
    incremental_ci: tuple[float, float]
    incremental_p_value: float
    max_library_similarity: float
    score_coverage: float
    model: str
    label_convention: str
    runtime_versions: tuple[tuple[str, str], ...]
    scope: str = "development"
    portfolio_performance_verified: bool = False

    @property
    def report_hash(self):
        return canonical_hash(asdict(self))


def _correlation(left, right):
    left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    left, right = left - left.mean(), right - right.mean()
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(np.clip(np.dot(left, right) / denominator, -1, 1)) if denominator else 0.0


def _rank_ic(left, right):
    return _correlation(cross_sectional_rank(np.asarray(left)[None, :])[0],
                        cross_sectional_rank(np.asarray(right)[None, :])[0])


def block_bootstrap_mean(values, *, block_length: int, samples: int, seed: int):
    """Circular moving-block bootstrap and centered-null one-sided mean test.

    This is a development diagnostic, not independent-trial certification. Calendar
    gaps are handled by the caller; short samples with fewer than two blocks fail.
    """
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ContractError("bootstrap needs a finite one-dimensional series")
    if block_length < 1 or samples < 1 or len(values) < 2 * block_length:
        raise ContractError("need at least two blocks for dependence-aware uncertainty")
    rng = np.random.default_rng(seed)
    center = float(values.mean())
    means = np.empty(samples)
    blocks = (len(values) + block_length - 1) // block_length
    offsets = np.arange(block_length)
    for index in range(samples):
        starts = rng.integers(0, len(values), size=blocks)
        selection = ((starts[:, None] + offsets) % len(values)).ravel()[:len(values)]
        means[index] = values[selection].mean()
    interval = tuple(float(x) for x in np.quantile(means, [.025, .975]))
    p_value = float((1 + np.count_nonzero(means - center >= center)) / (samples + 1))
    return interval, p_value


def _fit_predict(kind, x_train, y_train, x_test, groups, config):
    """All transformations and model fitting see training rows only."""
    if kind in {"ridge", "elastic_net"}:
        from sklearn.linear_model import ElasticNet, Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        model = (Ridge(alpha=config.regularization) if kind == "ridge" else
                 ElasticNet(alpha=config.regularization, l1_ratio=.5, max_iter=5000,
                            random_state=config.seed))
        pipeline = make_pipeline(StandardScaler(), model)
        pipeline.fit(x_train, y_train)
        return pipeline.predict(x_test)
    from lightgbm import LGBMRanker
    # Labels are within-date ordinal relevance, not globally binned future returns.
    relevance = np.minimum(30, np.floor(y_train * 30)).astype(int)
    model = LGBMRanker(n_estimators=100, num_leaves=7, learning_rate=.03,
                      min_child_samples=20, reg_lambda=config.regularization,
                      random_state=config.seed, n_jobs=1, verbosity=-1,
                      deterministic=True, force_col_wise=True)
    model.fit(x_train, relevance, group=groups)
    return model.predict(x_test)


class WalkForwardEvaluator:
    """Same-sample baseline/augmented comparison, with outcome-interval purging."""

    def __init__(self, config: WalkForwardConfig | None = None):
        self.config = config or WalkForwardConfig()

    def evaluate(self, formula: Formula, panel: ResearchPanel, labels: ForwardLabels,
                 *, snapshot_hash: str, baseline: Mapping[str, Formula] | None = None):
        if not snapshot_hash or labels.values.shape != panel.shape:
            raise ContractError("evaluation needs snapshot identity and aligned labels")
        config = self.config
        baseline = dict(baseline or {})
        score = formula.evaluate_panel(panel)
        library = [baseline[name].evaluate_panel(panel) for name in sorted(baseline)]
        all_features = np.stack([*library, score], axis=-1)
        target_ranks = cross_sectional_rank(labels.values)
        valid_rows = (np.isfinite(all_features).all(axis=2)
                      & np.isfinite(labels.values) & panel.eligible)
        # Groups must contain enough eligible stocks both in training and testing.
        valid_rows[valid_rows.sum(axis=1) < config.min_assets] = False
        folds, indices, raw_ics, base_ics, aug_ics = [], [], [], [], []
        similarity_by_library = [[] for _ in library]
        prediction_panel = np.full(panel.shape, np.nan)
        n_dates = len(panel.dates)
        # Warm-up labels need their full endpoint before the first fold starts.
        first_test = config.min_train_days + labels.horizon + 1 + config.embargo_days
        for test_start in range(first_test, n_dates, config.test_days):
            test_end = min(test_start + config.test_days, n_dates)
            train_dates = np.array([
                end is not None and end < test_start - config.embargo_days
                for end in labels.end_indices
            ])
            train_dates[test_start:] = False
            train_mask = valid_rows & train_dates[:, None]
            test_mask = valid_rows.copy()
            test_mask[:test_start] = False
            test_mask[test_end:] = False
            if train_mask.sum() < config.min_train_rows or not test_mask.any():
                continue
            train_day_indices = np.flatnonzero(train_mask.any(axis=1))
            if len(train_day_indices) < config.min_train_days:
                continue
            x_train, y_train = all_features[train_mask], target_ranks[train_mask]
            x_test = all_features[test_mask]
            groups = train_mask.sum(axis=1)
            groups = groups[groups > 0]
            augmented = _fit_predict(config.model, x_train, y_train, x_test, groups, config)
            base = (_fit_predict(config.model, x_train[:, :-1], y_train, x_test[:, :-1],
                                 groups, config) if library else np.zeros(len(x_test)))
            augmented_panel, base_panel = np.full(panel.shape, np.nan), np.full(panel.shape, np.nan)
            augmented_panel[test_mask], base_panel[test_mask] = augmented, base
            prediction_panel[test_mask] = augmented
            fold_raw, fold_base, fold_aug = [], [], []
            for day in range(test_start, test_end):
                mask = test_mask[day]
                if mask.sum() < config.min_assets:
                    continue
                outcomes = labels.values[day, mask]
                if np.std(outcomes) == 0:
                    continue
                raw = _rank_ic(score[day, mask], outcomes)
                b = _rank_ic(base_panel[day, mask], outcomes)
                a = _rank_ic(augmented_panel[day, mask], outcomes)
                indices.append(day)
                raw_ics.append(raw)
                base_ics.append(b)
                aug_ics.append(a)
                fold_raw.append(raw)
                fold_base.append(b)
                fold_aug.append(a)
                for index, existing in enumerate(library):
                    similarity_by_library[index].append(abs(_correlation(score[day, mask],
                                                                          existing[day, mask])))
            if fold_raw:
                folds.append(FoldEvidence(
                    int(train_day_indices[0]), int(train_day_indices[-1]),
                    max(labels.end_indices[i] for i in train_day_indices),
                    test_start, test_end - 1, int(train_mask.sum()), int(test_mask.sum()),
                    len(fold_raw), float(np.mean(fold_raw)), float(np.mean(fold_base)),
                    float(np.mean(fold_aug)), float(np.mean(np.array(fold_aug) - fold_base)),
                ))
        if len(folds) < config.min_folds:
            raise ContractError("insufficient usable walk-forward folds; no evidence manufactured")
        incremental = np.asarray(aug_ics) - np.asarray(base_ics)
        # Gaps are not compressed into fake adjacent sessions for overlapping labels.
        # Use the longest consecutive diagnostic segment, disclosing all measured days.
        segments, start = [], 0
        for index in range(1, len(indices)):
            if indices[index] != indices[index-1] + 1:
                segments.append(incremental[start:index])
                start = index
        segments.append(incremental[start:])
        longest = max(segments, key=len)
        ci, p_value = block_bootstrap_mean(longest, block_length=labels.horizon + 1,
                                          samples=config.bootstrap_samples, seed=config.seed)
        diagnostic_subset = len(longest) != len(incremental)
        if diagnostic_subset:
            # Do not pair a subset CI/p-value with the full-series effect claim.
            raise ContractError("gapped IC calendar: use a predeclared contiguous evaluation interval")
        eligible_count = int(np.count_nonzero(panel.eligible[first_test:]))
        measured_count = int(np.count_nonzero(np.isfinite(prediction_panel[first_test:])))
        report = DevelopmentReport(
            formula_hash=formula.formula_hash, snapshot_hash=snapshot_hash,
            panel_hash=panel.content_hash, labels_hash=labels.content_hash,
            policy_hash=canonical_hash(asdict(config)),
            library_hash=canonical_hash({name: baseline[name].formula_hash for name in sorted(baseline)}),
            horizon=labels.horizon, folds=tuple(folds), measured_date_indices=tuple(indices),
            daily_rank_ic=tuple(raw_ics), daily_baseline_rank_ic=tuple(base_ics),
            daily_augmented_rank_ic=tuple(aug_ics),
            daily_incremental_rank_ic=tuple(float(x) for x in incremental),
            rank_ic=float(np.mean(raw_ics)), incremental_rank_ic=float(incremental.mean()),
            incremental_ci=ci, incremental_p_value=p_value,
            max_library_similarity=max((float(np.mean(x)) for x in similarity_by_library if x), default=0),
            score_coverage=measured_count / eligible_count if eligible_count else 0,
            model=config.model, label_convention=labels.convention,
            runtime_versions=tuple((name, version(name)) for name in
                                   ("numpy", "scikit-learn", "lightgbm")
                                   if name != "lightgbm" or config.model == "lightgbm"),
        )
        return report, prediction_panel
