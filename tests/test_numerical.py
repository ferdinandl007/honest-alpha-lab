"""Generated numerical fixtures are correctness tests, never alpha benchmarks."""
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

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
                          {"x": x, "z": z, "negative_x": -x}, np.ones_like(x, dtype=bool),
                          open_at=tuple(datetime.combine(d, time(14), UTC) for d in dates),
                          decision_at=tuple(datetime.combine(d, time(21), UTC) for d in dates))
    labels = ForwardLabels.for_panel(panel, horizon=1, values=outcomes,
                                     end_indices=tuple(i + 2 if i + 2 < n else None for i in range(n)))
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
    changed_panel = replace(panel, fields=fields)
    changed_labels = ForwardLabels.for_panel(changed_panel, horizon=labels.horizon,
                                            values=outcomes, end_indices=labels.end_indices)
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


def test_evaluator_rejects_unbound_reordered_or_shifted_labels():
    panel, labels, config = fixture()
    evaluator = WalkForwardEvaluator(config)
    for other in (replace(panel, assets=tuple(reversed(panel.assets))),
                  replace(panel, dates=tuple(d + timedelta(days=1) for d in panel.dates))):
        with pytest.raises(ContractError, match="bound labels"):
            evaluator.evaluate(Formula.parse("x"), other, labels, snapshot_hash="fixture")
    with pytest.raises(ContractError, match="bound labels"):
        evaluator.evaluate(Formula.parse("x"), panel,
                           replace(labels, source_panel_hash=None), snapshot_hash="fixture")
    with pytest.raises(ContractError, match="explicit"):
        evaluator.evaluate(Formula.parse("x"), replace(panel, open_at=None, decision_at=None),
                           labels, snapshot_hash="fixture")


def test_labels_reject_understated_horizon_and_unknown_convention():
    panel, labels, _ = fixture()
    with pytest.raises(ContractError, match="endpoint"):
        replace(labels, horizon=5)
    with pytest.raises(ContractError, match="unsupported"):
        ForwardLabels.for_panel(panel, horizon=1, values=labels.values,
                                end_indices=labels.end_indices, convention="arbitrary")


def test_evidence_propagates_unknown_input_clock_without_claiming_as_run():
    panel, labels, config = fixture()
    report, _ = WalkForwardEvaluator(config).evaluate(Formula.parse("x"), panel, labels,
                                                     snapshot_hash="fixture")
    assert report.schema_version == 2
    assert report.label_source_panel_hash == panel.content_hash
    assert report.input_provenance == {"status": "unknown"}
