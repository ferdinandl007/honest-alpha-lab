"""Generated numerical fixtures are correctness tests, never alpha benchmarks."""
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.numerical import WalkForwardConfig, WalkForwardEvaluator, block_bootstrap_mean
from honest_alpha_lab.panel import ForwardLabels, ResearchPanel


def fixture():
    rng = np.random.default_rng(91)
    n, assets = 90, 8
    x, z = rng.normal(size=(2, n, assets))
    outcomes = .1 * x + .1 * z + rng.normal(size=x.shape) * .02
    outcomes[-2:] = np.nan
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(n))
    panel = ResearchPanel(dates, tuple(str(i) for i in range(assets)),
                          {"x": x, "z": z, "negative_x": -x}, np.ones_like(x, dtype=bool))
    labels = ForwardLabels(1, outcomes, tuple(i + 2 if i + 2 < n else None for i in range(n)))
    config = WalkForwardConfig(min_train_days=20, test_days=10, min_assets=4,
                               min_train_rows=40, bootstrap_samples=99)
    return panel, labels, config


def test_actual_label_end_is_purged_not_only_row_date():
    panel, labels, config = fixture()
    report, scores = WalkForwardEvaluator(config).evaluate(
        Formula.parse("rank(x)"), panel, labels, snapshot_hash="test-only-fixture",
        baseline={"z": Formula.parse("rank(z)")})
    assert len(report.folds) >= 2
    assert report.scope == "development"
    assert not report.portfolio_performance_verified
    for fold in report.folds:
        assert fold.last_training_label_end_index < fold.test_start_index - config.embargo_days
        assert fold.train_end_index < fold.last_training_label_end_index
    assert scores.shape == panel.shape
    assert report.incremental_rank_ic == pytest.approx(np.mean(
        np.array(report.daily_augmented_rank_ic) - report.daily_baseline_rank_ic))


def test_future_features_and_outcomes_do_not_change_earlier_fold_predictions():
    panel, labels, config = fixture()
    evaluator = WalkForwardEvaluator(config)
    first, predictions = evaluator.evaluate(Formula.parse("x"), panel, labels, snapshot_hash="fixture")
    fields = {name: values.copy() for name, values in panel.fields.items()}
    for field in fields.values():
        field[65:] *= -100
    outcomes = labels.values.copy()
    outcomes[65:] *= -10
    changed_panel = ResearchPanel(panel.dates, panel.assets, fields, panel.eligible)
    changed_labels = ForwardLabels(labels.horizon, outcomes, labels.end_indices)
    second, changed = evaluator.evaluate(Formula.parse("x"), changed_panel, changed_labels, snapshot_hash="changed")
    np.testing.assert_allclose(predictions[:65], changed[:65], equal_nan=True)
    assert first.folds[0] == second.folds[0]


def test_sign_flipped_duplicate_is_detected_and_increment_not_raw_ic():
    panel, labels, config = fixture()
    report, _ = WalkForwardEvaluator(config).evaluate(
        Formula.parse("negative_x"), panel, labels, snapshot_hash="fixture",
        baseline={"known": Formula.parse("x")})
    assert report.max_library_similarity == pytest.approx(1)
    assert report.incremental_rank_ic == pytest.approx(0)
    assert report.rank_ic != pytest.approx(0)


def test_missing_candidate_never_manufactures_evidence():
    panel, labels, config = fixture()
    with pytest.raises(ContractError, match="insufficient"):
        WalkForwardEvaluator(config).evaluate(Formula.parse("/ x 0"), panel, labels, snapshot_hash="fixture")


def test_bootstrap_zero_effect_and_short_series():
    ci, p = block_bootstrap_mean([0] * 20, block_length=3, samples=99, seed=1)
    assert ci == (0, 0)
    assert p == 1
    with pytest.raises(ContractError):
        block_bootstrap_mean([1, 2], block_length=2, samples=99, seed=1)


@pytest.mark.parametrize("model", ["ridge", "elastic_net", "lightgbm"])
def test_conventional_model_interfaces_execute(model):
    panel, labels, config = fixture()
    report, _ = WalkForwardEvaluator(replace(config, model=model)).evaluate(
        Formula.parse("x"), panel, labels, snapshot_hash="test-only-fixture")
    assert report.model == model
    assert np.isfinite(report.rank_ic)
