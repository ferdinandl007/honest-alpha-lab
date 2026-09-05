"""Numerical-only regime and alpha-combination baselines."""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, log
from statistics import mean
from typing import Mapping, Sequence

from .contracts import ContractError


def _softmax(scores: Sequence[float]) -> tuple[float, ...]:
    maximum = max(scores)
    values = [exp(score - maximum) for score in scores]
    total = sum(values) or 1.0
    return tuple(value / total for value in values)


class GaussianHMMBaseline:
    """Small diagonal Gaussian HMM forward-filter baseline.

    It deliberately exposes probabilities instead of hard market labels. In production,
    this interface can be backed by hmmlearn or a Markov-switching implementation.
    """

    def __init__(self, states: int = 2) -> None:
        if states < 2:
            raise ContractError("regime model needs at least two states")
        self.states = states
        self._means: tuple[float, ...] = ()
        self._variance = 1.0
        self._transition: tuple[tuple[float, ...], ...] = ()

    def fit(self, returns: Sequence[float]) -> "GaussianHMMBaseline":
        if len(returns) < self.states * 2:
            raise ContractError("not enough observations to fit regime baseline")
        ordered = sorted(float(value) for value in returns)
        buckets = [
            ordered[
                i * len(ordered) // self.states : (i + 1) * len(ordered) // self.states
            ]
            for i in range(self.states)
        ]
        self._means = tuple(mean(bucket) for bucket in buckets)
        variance = sum((value - mean(returns)) ** 2 for value in returns) / max(
            len(returns) - 1, 1
        )
        self._variance = max(variance, 1e-8)
        stay = 0.95
        switch = (1 - stay) / (self.states - 1)
        self._transition = tuple(
            tuple(stay if i == j else switch for j in range(self.states))
            for i in range(self.states)
        )
        return self

    def filter(self, returns: Sequence[float]) -> tuple[tuple[float, ...], ...]:
        if not self._means:
            raise ContractError("fit the regime model before filtering")
        previous = tuple(1 / self.states for _ in range(self.states))
        result = []
        for value in returns:
            prior = tuple(
                sum(previous[i] * self._transition[i][j] for i in range(self.states))
                for j in range(self.states)
            )
            log_likelihood = tuple(
                -0.5 * (value - regime_mean) ** 2 / self._variance
                for regime_mean in self._means
            )
            posterior = _softmax(
                tuple(log(prior[i] or 1e-12) + log_likelihood[i] for i in range(self.states))
            )
            result.append(posterior)
            previous = posterior
        return tuple(result)


@dataclass(frozen=True, slots=True)
class MarketRegimeObservation:
    """Observable inputs used to infer, rather than hardcode, market regimes."""

    return_value: float
    volatility: float
    credit_spread: float
    breadth: float
    average_correlation: float
    liquidity: float
    rate_level: float

    def vector(self) -> tuple[float, ...]:
        return (
            self.return_value,
            self.volatility,
            self.credit_spread,
            self.breadth,
            self.average_correlation,
            self.liquidity,
            self.rate_level,
        )


class MultivariateGaussianHMMBaseline:
    """Diagonal-Gaussian HMM baseline for a multi-signal market state vector.

    It exposes transition-aware posterior probabilities, never hard bull/bear labels.
    The deterministic initialisation is intended as a transparent Phase-1 reference;
    production can replace it with an EM or Markov-switching implementation.
    """

    def __init__(self, states: int = 4) -> None:
        if states < 2:
            raise ContractError("regime model needs at least two states")
        self.states = states
        self._means: tuple[tuple[float, ...], ...] = ()
        self._variances: tuple[tuple[float, ...], ...] = ()
        self._transition: tuple[tuple[float, ...], ...] = ()

    def fit(
        self, observations: Sequence[MarketRegimeObservation]
    ) -> "MultivariateGaussianHMMBaseline":
        if len(observations) < self.states * 2:
            raise ContractError("not enough observations to fit regime baseline")
        vectors = tuple(observation.vector() for observation in observations)
        ordered = sorted(vectors, key=lambda vector: vector[0])
        groups = [
            ordered[
                state * len(ordered) // self.states : (state + 1)
                * len(ordered)
                // self.states
            ]
            for state in range(self.states)
        ]
        self._means = tuple(
            tuple(
                sum(row[column] for row in group) / len(group)
                for column in range(len(vectors[0]))
            )
            for group in groups
        )
        self._variances = tuple(
            tuple(
                max(
                    sum((row[column] - state_mean[column]) ** 2 for row in group)
                    / max(len(group) - 1, 1),
                    1e-8,
                )
                for column in range(len(vectors[0]))
            )
            for group, state_mean in zip(groups, self._means)
        )
        stay = 0.95
        switch = (1 - stay) / (self.states - 1)
        self._transition = tuple(
            tuple(stay if i == j else switch for j in range(self.states))
            for i in range(self.states)
        )
        return self

    def filter(
        self, observations: Sequence[MarketRegimeObservation]
    ) -> tuple[tuple[float, ...], ...]:
        if not self._means:
            raise ContractError("fit the regime model before filtering")
        previous = tuple(1 / self.states for _ in range(self.states))
        posteriors = []
        for observation in observations:
            vector = observation.vector()
            prior = tuple(
                sum(previous[i] * self._transition[i][j] for i in range(self.states))
                for j in range(self.states)
            )
            log_likelihood = tuple(
                -0.5
                * sum(
                    log(state_variance[column])
                    + (value - state_mean[column]) ** 2 / state_variance[column]
                    for column, value in enumerate(vector)
                )
                for state_mean, state_variance in zip(self._means, self._variances)
            )
            posterior = _softmax(
                tuple(
                    log_likelihood[i] + log(prior[i] or 1e-12)
                    for i in range(self.states)
                )
            )
            posteriors.append(posterior)
            previous = posterior
        return tuple(posteriors)


class RollingClusterBaseline:
    """Non-Markov comparison baseline using nearest rolling-window centroids."""

    def __init__(self, clusters: int = 2, window: int = 20) -> None:
        if clusters < 2 or window <= 0:
            raise ContractError(
                "clusters and window must be positive; at least two clusters required"
            )
        self.clusters, self.window = clusters, window
        self._centroids: tuple[float, ...] = ()

    def fit(self, returns: Sequence[float]) -> "RollingClusterBaseline":
        if len(returns) < self.clusters:
            raise ContractError("not enough observations to fit cluster baseline")
        ordered = sorted(float(value) for value in returns)
        self._centroids = tuple(
            ordered[i * len(ordered) // self.clusters] for i in range(self.clusters)
        )
        return self

    def predict_proba(self, returns: Sequence[float]) -> tuple[tuple[float, ...], ...]:
        if not self._centroids:
            raise ContractError("fit the cluster model before predicting")
        return tuple(
            _softmax(tuple(-abs(value - centroid) for centroid in self._centroids))
            for value in returns
        )


class EqualWeightAlphaEnsemble:
    """Transparent baseline combiner; weights are numerical and equal by construction."""

    def transform(
        self, alpha_scores: Mapping[str, Sequence[float]]
    ) -> tuple[float, ...]:
        if not alpha_scores:
            raise ContractError("at least one alpha is required")
        lengths = {len(scores) for scores in alpha_scores.values()}
        if len(lengths) != 1 or 0 in lengths:
            raise ContractError("alpha score vectors must have equal non-zero length")
        names = tuple(alpha_scores)
        return tuple(
            sum(alpha_scores[name][i] for name in names) / len(names)
            for i in range(next(iter(lengths)))
        )


@dataclass(frozen=True, slots=True)
class RidgeRanker:
    """Dependency-free ridge-like ranker using fixed-step gradient descent.

    This is a deterministic reference implementation. A production runner can replace
    it with scikit-learn while retaining the same fit/predict contract.
    """

    regularization: float = 1.0
    steps: int = 500
    learning_rate: float = 0.01

    def fit(
        self, features: Sequence[Sequence[float]], target: Sequence[float]
    ) -> tuple[float, ...]:
        if not features or len(features) != len(target):
            raise ContractError("ridge inputs must be non-empty and aligned")
        width = len(features[0])
        if width == 0 or any(len(row) != width for row in features):
            raise ContractError("ridge features must be rectangular")
        weights = [0.0] * width
        for _ in range(self.steps):
            gradient = [0.0] * width
            for row, actual in zip(features, target):
                error = sum(w * x for w, x in zip(weights, row)) - actual
                for j, value in enumerate(row):
                    gradient[j] += error * value
            scale = 2 / len(features)
            for j in range(width):
                gradient[j] = scale * gradient[j] + 2 * self.regularization * weights[j]
                weights[j] -= self.learning_rate * gradient[j]
        return tuple(weights)

    @staticmethod
    def predict(
        features: Sequence[Sequence[float]], weights: Sequence[float]
    ) -> tuple[float, ...]:
        if any(len(row) != len(weights) for row in features):
            raise ContractError("prediction feature width does not match weights")
        return tuple(
            sum(value * weight for value, weight in zip(row, weights))
            for row in features
        )
