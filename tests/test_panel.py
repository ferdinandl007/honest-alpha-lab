"""Numerical correctness fixtures, not financial-performance benchmarks."""
from datetime import date, timedelta

import numpy as np
import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.panel import (
    ForwardLabels,
    ResearchPanel,
    estimate_incremental_beta,
    next_open_residual_labels,
)


def panel(values, eligible=None, **fields):
    values = np.asarray(values, dtype=float)
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(len(values)))
    return ResearchPanel(dates, tuple(f"asset-{i}" for i in range(values.shape[1])),
                         {"x": values, **fields},
                         np.ones(values.shape, dtype=bool) if eligible is None else eligible)


def test_ranks_average_ties_and_are_per_date():
    p = panel([[7, 7, 7], [9, 2, 2], [np.nan, 1, 2]])
    np.testing.assert_allclose(Formula.parse("rank(x)").evaluate_panel(p),
                               [[.5, .5, .5], [1, .25, .25], [np.nan, 0, 1]], equal_nan=True)
    assert Formula.parse("rank(x)").evaluate({"x": [7, 7, 7]}) == [.5, .5, .5]


def test_time_windows_never_cross_security_and_preserve_warmup():
    p = panel([[1, 100], [2, 200], [3, np.nan], [4, 400]])
    np.testing.assert_allclose(Formula.parse("lag(x, 1)").evaluate_panel(p),
                               [[np.nan, np.nan], [1, 100], [2, 200], [3, np.nan]], equal_nan=True)
    np.testing.assert_allclose(Formula.parse("ts_mean(x, 2)").evaluate_panel(p),
                               [[np.nan, np.nan], [1.5, 150], [2.5, np.nan], [3.5, np.nan]], equal_nan=True)
    np.testing.assert_allclose(Formula.parse("ts_delta(x, 2)").evaluate_panel(p),
                               [[np.nan, np.nan], [np.nan, np.nan], [2, np.nan], [2, 200]], equal_nan=True)


def test_future_change_cannot_change_past_features():
    a = np.arange(30, dtype=float).reshape(10, 3)
    b = a.copy()
    b[7:] = -1000
    for expression in ("rank(x)", "zscore(x)", "rank(ts_delta(x, 2))", "ts_mean(rank(x), 3)"):
        f = Formula.parse(expression)
        np.testing.assert_allclose(f.evaluate_panel(panel(a))[:7], f.evaluate_panel(panel(b))[:7], equal_nan=True)


def test_missing_divisor_and_ineligible_assets_never_get_scores():
    p = panel([[1, 2, 3]], eligible=np.array([[True, False, True]]))
    assert np.isnan(Formula.parse("/ x 0").evaluate_panel(p)).all()
    np.testing.assert_allclose(Formula.parse("rank(x)").evaluate_panel(p), [[0, np.nan, 1]], equal_nan=True)
    np.testing.assert_allclose(Formula.parse("winsorize(x, 0.5)").evaluate_panel(p), [[.5, np.nan, .5]], equal_nan=True)


@pytest.mark.parametrize("expression", ["lag(x, x)", "ts_delta(x, 1.5)", "ts_mean(x, 0)", "lag(x, 2521)", "rank(" * 100 + "x" + ")" * 100])
def test_invalid_or_unbounded_windows_fail_during_parse(expression):
    with pytest.raises(ContractError):
        Formula.parse(expression)


def test_canonical_duplicates_and_noncommutative_distinction():
    assert Formula.parse("  + x y ").formula_hash == Formula.parse("+ y x").formula_hash
    assert Formula.parse("rank(x)").formula_hash == Formula.parse("rank( x )").formula_hash
    assert Formula.parse("- x y").formula_hash != Formula.parse("- y x").formula_hash


def test_panel_copies_inputs_and_cannot_be_mutated():
    source = np.ones((2, 2))
    p = panel(source)
    source[:] = 3
    assert p.fields["x"][0, 0] == 1
    with pytest.raises(ValueError):
        p.fields["x"].setflags(write=True)
    with pytest.raises(TypeError):
        p.fields["new"] = source
    with pytest.raises(ContractError):
        panel([[np.inf]])


def test_next_open_targets_exclude_decision_day_and_keep_terminal_proceeds():
    stock = np.array([[100], [200], [220], [0], [0]], dtype=float)
    reference = np.ones_like(stock) * 100
    eligible = np.array([[True], [True], [True], [False], [False]])
    p = panel(stock, eligible, total_return_open=stock,
              sector_total_return_open=reference, market_total_return_open=reference)
    labels = next_open_residual_labels(p, 1, np.zeros(p.shape))
    assert labels.values[0, 0] == pytest.approx(.1)  # 200 -> 220, not 100 -> 200
    assert labels.values[1, 0] == -1  # explicit terminal loss is not deleted
    assert labels.end_indices == (2, 3, 4, None, None)
    assert np.isnan(labels.values[-1, 0])


def test_beta_fit_has_no_future_information_and_full_window_required():
    m = np.array([100, 101, 99, 102, 104, 103, 106, 110], dtype=float)[:, None]
    sector = np.ones_like(m) * 100
    p = panel(m, total_return_close=m, sector_total_return_close=sector, market_total_return_close=m)
    beta = estimate_incremental_beta(p, window=3)
    assert np.isnan(beta[:3]).all()
    np.testing.assert_allclose(beta[3:], 1)
    changed = m.copy()
    changed[-2:] = 1000
    other = panel(changed, total_return_close=changed, sector_total_return_close=sector, market_total_return_close=m)
    np.testing.assert_allclose(estimate_incremental_beta(other, 3)[:6], beta[:6], equal_nan=True)


def test_labels_reject_made_up_end_times():
    with pytest.raises(ContractError):
        ForwardLabels(1, np.ones((2, 2)), (None, None))
