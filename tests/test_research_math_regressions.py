"""Hand-calculated research regressions; not financial performance evidence."""
import math
from types import SimpleNamespace

import pytest

from honest_alpha_lab.alternative_data import AlternativeDatasetCreationAgent, DatasetStatus
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.models import MarketRegimeObservation, MultivariateGaussianHMMBaseline


@pytest.mark.parametrize("expression", ["rank(x)", "zscore(x)", "lag(x, 1)", "ts_mean(x, 20)", "ts_delta(x, 2)"])
@pytest.mark.parametrize("mapped", [False, True])
def test_both_feature_builders_reject_series_formulas_before_emitting_observations(expression, mapped):
    candidate = SimpleNamespace(status=DatasetStatus.APPROVED, dataset=SimpleNamespace(dataset_id="d"))
    registry = SimpleNamespace(get=lambda _: candidate)
    definition = SimpleNamespace(dataset_candidate_id="d", formula=expression)
    agent = AlternativeDatasetCreationAgent()
    with pytest.raises(ContractError, match="scalar"):
        if mapped:
            agent.build_mapped_feature(definition, (), registry,
                SimpleNamespace(proposal=SimpleNamespace(dataset_id="d")), None, None, None)
        else:
            agent.build_feature(definition, (), registry)


def test_scalar_winsorization_accepts_fractional_limit_and_zero_division_is_missing():
    assert Formula.parse("winsorize(x, 0.5)").evaluate({"x": [-2, .2, 3]}) == [-.5, .2, .5]
    assert math.isnan(Formula.parse("/ x 0").evaluate({"x": [1]})[0])


@pytest.mark.parametrize("expression", ["winsorize(/ x 0, 0.5)", "min(x, / x 0)", "max(x, / x 0)"])
def test_scalar_wrappers_cannot_turn_missing_arithmetic_into_a_signal(expression):
    assert math.isnan(Formula.parse(expression).evaluate({"x": [1]})[0])


def test_multivariate_gaussian_filter_includes_state_variance_normalizer():
    model = MultivariateGaussianHMMBaseline(states=2)
    # Equal means/priors. Only the first feature differs in variance: 1 vs 4.
    # At the shared mean, normal densities have odds 2:1, not 1:1.
    model._means = ((0.,) * 7, (0.,) * 7)
    model._variances = ((1.,) * 7, (4., 1., 1., 1., 1., 1., 1.))
    model._transition = ((.5, .5), (.5, .5))
    result = model.filter((MarketRegimeObservation(*([0.] * 7)),))[0]
    assert result == pytest.approx((2 / 3, 1 / 3))
