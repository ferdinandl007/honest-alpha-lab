"""Synthetic integration checks, not evidence of market predictability."""
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest
from test_numerical import fixture

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.numerical import WalkForwardEvaluator
from honest_alpha_lab.panel import ForwardLabels, ResearchPanel
from honest_alpha_lab.regimes import (
    FittedRegimeModel,
    RegimeConfig,
    causal_market_features,
    conditional_ic,
)


def market_fixture():
    panel, labels, config = fixture()
    rng = np.random.default_rng(19)
    prices = 100 * np.exp(np.cumsum(rng.normal(0, .01, panel.shape), axis=0))
    panel = replace(panel, fields={**panel.fields, "total_return_close": prices})
    labels = ForwardLabels.for_panel(panel, horizon=labels.horizon, values=labels.values,
                                     end_indices=labels.end_indices)
    return panel, labels, replace(config, min_train_days=35, regime_feature_window=3)


def evaluate(panel, labels, config):
    return WalkForwardEvaluator(config).evaluate(
        Formula.parse("x"), panel, labels, snapshot_hash="synthetic")


def test_both_methods_fit_training_only_and_report_aligned_weighted_ic(monkeypatch):
    panel, labels, config = market_fixture()
    calls = []
    original = FittedRegimeModel.fit

    def record(self, dates, features):
        calls.append(tuple(dates))
        return original(self, dates, features)

    monkeypatch.setattr(FittedRegimeModel, "fit", record)
    report, _ = evaluate(panel, labels, config)
    assert len(report.regime_reports) == 2 * len(report.folds)
    assert {item.method for item in report.regime_reports} == {"hmm", "gaussian_mixture"}
    for item, fitted_dates in zip(report.regime_reports, calls, strict=True):
        fold = report.folds[item.fold_index]
        assert item.status in {"ok", "not_converged"}
        assert fitted_dates == tuple(panel.dates[i] for i in item.training_date_indices)
        assert item.training_date_indices[-1] == fold.train_end_index
        assert item.training_date_indices[-1] < fold.test_start_index
        positions = [report.measured_date_indices.index(i) for i in item.measured_date_indices]
        np.testing.assert_allclose(np.sum(item.probabilities, axis=1), 1)
        for source, result in (
            (report.daily_rank_ic, item.conditional_rank_ic),
            (report.daily_baseline_rank_ic, item.conditional_baseline_rank_ic),
            (report.daily_augmented_rank_ic, item.conditional_augmented_rank_ic),
        ):
            assert result == conditional_ic(np.asarray(source)[positions], item.probabilities,
                                            allow_empty=True)
        assert item.iterations > 0
        assert isinstance(item.iteration_limit_reached, bool)
        probabilities = np.asarray(item.probabilities)
        increments = np.asarray(report.daily_incremental_rank_ic)[positions]
        for state in item.conditional_incremental_rank_ic:
            weights = probabilities[:, state["state"]]
            if weights.sum() > 0:
                assert state["rank_ic"] == pytest.approx(np.average(increments, weights=weights))
    assert report.report_hash


def test_market_features_and_oos_probabilities_are_prefix_invariant():
    panel, labels, config = market_fixture()
    source, features = causal_market_features(panel, window=3)
    prefix = ResearchPanel(panel.dates[:65], panel.assets,
                           {k: v[:65] for k, v in panel.fields.items()}, panel.eligible[:65],
                           open_at=panel.open_at[:65], decision_at=panel.decision_at[:65])
    prefix_source, prefix_features = causal_market_features(prefix, window=3)
    assert source == prefix_source == "total_return_close"
    np.testing.assert_allclose(features[:65], prefix_features, equal_nan=True)
    first, _ = evaluate(panel, labels, config)
    fields = {k: v.copy() for k, v in panel.fields.items()}
    fields["total_return_close"][65:] *= 100
    changed_panel = replace(panel, fields=fields)
    changed_labels = ForwardLabels.for_panel(changed_panel, horizon=labels.horizon,
                                            values=labels.values, end_indices=labels.end_indices)
    second, _ = evaluate(changed_panel, changed_labels, config)
    for before, after in zip(first.regime_reports, second.regime_reports, strict=True):
        if before.training_date_indices[-1] >= 65:
            continue
        count = sum(i < 65 for i in before.measured_date_indices)
        np.testing.assert_allclose(before.probabilities[:count], after.probabilities[:count])


def test_missing_insufficient_and_optout_are_explicit_and_policy_is_frozen():
    panel, labels, config = fixture()
    report, _ = evaluate(panel, labels, config)
    assert all(r.status == "missing" for r in report.regime_reports)
    assert all(r.diagnostic and not r.probabilities for r in report.regime_reports)
    disabled, _ = evaluate(panel, labels, replace(config, regime_configs=()))
    assert disabled.regime_reports == ()
    assert disabled.policy_hash != report.policy_hash
    with pytest.raises(FrozenInstanceError):
        config.regime_configs[0].states = 4
    with pytest.raises(ContractError):
        replace(config, regime_configs=list(config.regime_configs))
    panel, labels, config = market_fixture()
    insufficient, _ = evaluate(panel, labels, replace(config, regime_feature_window=80))
    assert all(r.status == "insufficient" for r in insufficient.regime_reports)


def test_unlabeled_tail_and_future_label_missingness_do_not_remove_predictions():
    panel, labels, config = fixture()
    config = replace(config, regime_configs=())
    original, scores = evaluate(panel, labels, config)
    assert np.isfinite(scores[-2:]).all()
    outcomes = labels.values.copy()
    # Remove labels in an OOS fold and every later session. This must neither
    # suppress this fold's predictions nor entirely unlabeled subsequent folds.
    cutoff = original.folds[2].test_start_index
    outcomes[cutoff:] = np.nan
    changed_labels = ForwardLabels.for_panel(panel, horizon=labels.horizon, values=outcomes,
                                            end_indices=labels.end_indices)
    changed, predictions = evaluate(panel, changed_labels, config)
    end = original.folds[2].test_end_index + 1
    np.testing.assert_allclose(scores[:end], predictions[:end], equal_nan=True)
    assert np.isfinite(predictions[cutoff:]).all()
    assert all(day < cutoff for day in changed.measured_date_indices)


def test_hmm_iteration_limit_is_not_tolerance_convergence():
    panel, _, _ = market_fixture()
    _, features = causal_market_features(panel, window=3)
    model = FittedRegimeModel(RegimeConfig(states=2, max_iterations=1))
    model.fit(panel.dates[3:60], features[3:60])
    assert not model.converged
    assert model.iterations == 1
    assert model.iteration_limit_reached


def test_zero_mass_state_is_explicit_without_breaking_existing_strict_api():
    assert conditional_ic([.2], [[1, 0]], allow_empty=True)[1] == {
        "state": 1, "rank_ic": None, "effective_days": 0.0}
    with pytest.raises(ContractError):
        conditional_ic([.2], [[1, 0]])


def test_missing_embargo_features_are_disclosed_and_not_silently_skipped():
    panel, labels, config = market_fixture()
    fields = dict(panel.fields)
    prices = fields["total_return_close"].copy()
    # First training interval ends at 34, first test starts at 42.
    prices[38] = np.nan
    fields["total_return_close"] = prices
    panel = replace(panel, fields=fields)
    labels = ForwardLabels.for_panel(panel, horizon=labels.horizon, values=labels.values,
                                     end_indices=labels.end_indices)
    report, _ = evaluate(panel, labels, config)
    for item in report.regime_reports[:2]:
        assert item.status == "missing"
        assert "between training" in item.diagnostic
        assert item.probabilities == ()


def test_market_feature_values_use_price_relatives_and_full_trailing_windows():
    panel, _, _ = market_fixture()
    source, features = causal_market_features(panel, window=3)
    prices = panel.fields[source]
    daily = (prices[1:] / prices[:-1] - 1).mean(axis=1)
    assert np.isnan(features[:3]).all()
    np.testing.assert_allclose(features[3], [daily[2], daily[:3].mean(), daily[:3].std()])
