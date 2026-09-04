"""Inference correctness fixtures only; no claims about market predictability."""
from datetime import date, timedelta

import numpy as np
import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.regimes import FittedRegimeModel, RegimeConfig, conditional_ic


@pytest.mark.parametrize("method", ["hmm", "gaussian_mixture"])
def test_future_append_cannot_change_past_posteriors(method):
    rng = np.random.default_rng(22)
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(120))
    features = rng.normal(size=(120, 3))
    features[30:60] += 3
    model = FittedRegimeModel(RegimeConfig(method=method, states=2, max_iterations=50))
    model.fit(dates[:90], features[:90])
    first = model.predict_proba(dates[90:100], features[90:100])
    changed = features[90:].copy()
    changed[10:] = 1000
    extended = model.predict_proba(dates[90:], changed)
    np.testing.assert_allclose(first, extended[:10])
    np.testing.assert_allclose(extended.sum(axis=1), 1)
    assert (extended >= 0).all()
    with pytest.raises(ContractError, match="follow"):
        model.predict_proba(dates[89:91], features[89:91])


def test_conditional_ic_uses_scores_and_reports_effective_days():
    result = conditional_ic([.1, -.1], [[.9, .1], [.1, .9]])
    assert result[0]["rank_ic"] == pytest.approx(.08)
    assert result[1]["rank_ic"] == pytest.approx(-.08)
    assert result[0]["effective_days"] == pytest.approx(1/.82)
    with pytest.raises(ContractError):
        conditional_ic([.1], [[.4, .4]])
