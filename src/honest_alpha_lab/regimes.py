"""Fitted conventional market-state models with causal out-of-sample inference."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy.special import logsumexp
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

from .contracts import ContractError


@dataclass(frozen=True)
class RegimeConfig:
    method: str = "hmm"
    states: int = 3
    max_iterations: int = 200
    seed: int = 0

    def __post_init__(self):
        if self.method not in {"hmm", "gaussian_mixture"}:
            raise ContractError("use HMM or a non-Markov Gaussian mixture")
        if type(self.states) is not int or self.states < 2:
            raise ContractError("at least two states are required")
        if type(self.max_iterations) is not int or self.max_iterations <= 0:
            raise ContractError("iteration budget must be positive")


class FittedRegimeModel:
    """Fit emissions/transitions on training data, then filter future observations.

    Neither HMM smoothed posteriors nor hindsight Viterbi labels are exposed.
    Calls are deterministic batches from the end-of-training posterior; to extend a
    batch, pass its full prefix again. This avoids hidden mutable filtering state.
    """

    def __init__(self, config: RegimeConfig | None = None):
        self.config = config or RegimeConfig()
        self._model = None
        self._scaler = None
        self._last_training_date = None
        self._last_training_probability = None
        self.converged = False

    @staticmethod
    def _validate(dates: Sequence[date], features):
        dates = tuple(dates)
        values = np.asarray(features, dtype=float)
        if (not dates or any(type(d) is not date for d in dates)
                or dates != tuple(sorted(set(dates)))):
            raise ContractError("regime observations require ordered unique session dates")
        if values.ndim != 2 or values.shape[0] != len(dates) or values.shape[1] == 0:
            raise ContractError("regime features must be date-by-feature")
        if not np.isfinite(values).all():
            raise ContractError("regime features must be finite; imputation must be predeclared")
        return dates, values

    def fit(self, dates: Sequence[date], features):
        dates, values = self._validate(dates, features)
        if len(dates) < max(20, self.config.states * 5):
            raise ContractError("insufficient training observations for latent states")
        self._scaler = StandardScaler().fit(values)
        training = self._scaler.transform(values)
        if self.config.method == "hmm":
            from hmmlearn.hmm import GaussianHMM
            self._model = GaussianHMM(n_components=self.config.states, covariance_type="diag",
                                      n_iter=self.config.max_iterations, random_state=self.config.seed,
                                      min_covar=1e-4).fit(training)
            self.converged = bool(self._model.monitor_.converged)
            self._last_training_probability = self._forward(training, initial=None)[-1]
        else:
            self._model = GaussianMixture(n_components=self.config.states, covariance_type="diag",
                                          max_iter=self.config.max_iterations,
                                          random_state=self.config.seed, reg_covar=1e-4).fit(training)
            self.converged = bool(self._model.converged_)
        self._last_training_date = dates[-1]
        return self

    def _forward(self, values, initial):
        # Public fitted attributes only, not hmmlearn's smoothed predict_proba.
        means = self._model.means_
        covariance = self._model.covars_
        if covariance.ndim == 3:
            covariance = np.diagonal(covariance, axis1=1, axis2=2)
        covariance = np.maximum(covariance, 1e-12)
        probabilities = []
        previous = initial
        for row in values:
            prior = self._model.startprob_ if previous is None else previous @ self._model.transmat_
            log_emission = -.5 * (np.log(2 * np.pi * covariance)
                                   + (row - means) ** 2 / covariance).sum(axis=1)
            with np.errstate(divide="ignore"):
                joint = np.log(prior) + log_emission
            normalizer = logsumexp(joint)
            if not np.isfinite(normalizer):
                raise ContractError("latent-state filter produced invalid evidence")
            previous = np.exp(joint - normalizer)
            probabilities.append(previous)
        return np.asarray(probabilities)

    def predict_proba(self, dates: Sequence[date], features):
        if self._model is None:
            raise ContractError("fit the regime model before inference")
        dates, values = self._validate(dates, features)
        if dates[0] <= self._last_training_date:
            raise ContractError("regime inference must follow the training interval")
        if values.shape[1] != self._scaler.n_features_in_:
            raise ContractError("regime feature width changed")
        transformed = self._scaler.transform(values)
        if self.config.method == "hmm":
            result = self._forward(transformed, self._last_training_probability)
        else:
            result = self._model.predict_proba(transformed)
        if not np.isfinite(result).all() or not np.allclose(result.sum(axis=1), 1):
            raise ContractError("invalid latent-state probabilities")
        return result


def conditional_ic(daily_ic, probabilities):
    """Probability-weighted daily IC and Kish effective sample size per state.

    Kish ESS describes concentration of probability weights, not correction for
    serial dependence. Separate block-based uncertainty is still required.
    """
    values, probabilities = np.asarray(daily_ic, dtype=float), np.asarray(probabilities, dtype=float)
    if (values.ndim != 1 or probabilities.ndim != 2
            or len(values) != len(probabilities) or len(values) == 0
            or not np.isfinite(values).all() or not np.isfinite(probabilities).all()
            or (np.abs(values) > 1).any() or (probabilities < 0).any()
            or not np.allclose(probabilities.sum(axis=1), 1, atol=1e-9, rtol=0)):
        raise ContractError("conditional IC needs aligned valid daily IC and state probabilities")
    mass = probabilities.sum(axis=0)
    if (mass <= 0).any():
        raise ContractError("cannot estimate a state without probability mass")
    return tuple({"state": index, "rank_ic": float((probabilities[:, index] * values).sum() / mass[index]),
                  "effective_days": float(mass[index] ** 2 / (probabilities[:, index] ** 2).sum())}
                 for index in range(probabilities.shape[1]))

