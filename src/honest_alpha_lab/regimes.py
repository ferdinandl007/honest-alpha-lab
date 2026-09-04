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
from .panel import ResearchPanel


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


@dataclass(frozen=True)
class RegimeFoldReport:
    """State IDs are local to this method and fold; never pooled across refits.

    Conditional means are descriptive, without serial-dependence uncertainty.
    Probabilities use information through each measured decision session.
    """

    method: str
    fold_index: int
    status: str
    diagnostic: str
    source_field: str | None = None
    training_date_indices: tuple[int, ...] = ()
    measured_date_indices: tuple[int, ...] = ()
    probabilities: tuple[tuple[float, ...], ...] = ()
    conditional_rank_ic: tuple[dict, ...] = ()
    conditional_baseline_rank_ic: tuple[dict, ...] = ()
    conditional_augmented_rank_ic: tuple[dict, ...] = ()
    conditional_incremental_rank_ic: tuple[dict, ...] = ()
    converged: bool | None = None
    iterations: int | None = None
    iteration_limit_reached: bool | None = None


def causal_market_features(panel: ResearchPanel, *, window: int = 20,
                           min_assets: int = 3):
    """Daily return, trailing mean and volatility of equal-weight price relatives.

    Prefer supplied market indices, then eligible stock total-return indices;
    prefer close to open within each source. No future prices, filling, or
    full-sample normalization. Both endpoints must be observed and eligible.
    A missing session invalidates the entire trailing window.
    """
    if type(window) is not int or window < 2:
        raise ContractError("market feature window must be an integer >= 2")
    if type(min_assets) is not int or min_assets < 1:
        raise ContractError("market features need a positive asset count")
    source = next((name for name in (
        "market_total_return_close", "market_total_return_open",
        "total_return_close", "total_return_open") if name in panel.fields), None)
    features = np.full((len(panel.dates), 3), np.nan)
    if source is None:
        return source, features
    prices = panel.fields[source]
    daily = np.full(len(panel.dates), np.nan)
    for day in range(1, len(daily)):
        valid = (panel.eligible[day - 1] & panel.eligible[day]
                 & np.isfinite(prices[day - 1]) & np.isfinite(prices[day])
                 & (prices[day - 1] > 0) & (prices[day] >= 0))
        required = 1 if source.startswith("market_") else min_assets
        if valid.sum() >= required:
            with np.errstate(over="ignore", invalid="ignore"):
                daily[day] = np.mean(prices[day, valid] / prices[day - 1, valid] - 1)
        if day >= window:
            trailing = daily[day - window + 1:day + 1]
            if np.isfinite(trailing).all():
                features[day] = daily[day], trailing.mean(), trailing.std()
    return source, features


def walk_forward_regime_reports(panel, folds, measured_indices, raw_ic, baseline_ic,
                                augmented_ic, configs, *, window=20, min_assets=3):
    """Fit only inside each supervised training interval, filter through the gap.

    Embargo and unmeasured sessions update the filter but are never fitted or
    included in conditional IC. Missing filter inputs are disclosed, not skipped.
    """
    if not configs:
        return ()
    source, features = causal_market_features(panel, window=window, min_assets=min_assets)
    reports = []
    measured = np.asarray(measured_indices)
    for fold_index, fold in enumerate(folds):
        selection = np.flatnonzero((measured >= fold.test_start_index)
                                   & (measured <= fold.test_end_index))
        days = measured[selection]
        # Use the contiguous available training suffix so HMM transitions always
        # mean one observed session, including after missing-price windows.
        end = fold.train_end_index
        start = end + 1
        while start > fold.train_start_index and np.isfinite(features[start - 1]).all():
            start -= 1
        training = tuple(range(start, end + 1))
        for config in configs:
            common = {"method": config.method, "fold_index": fold_index,
                      "source_field": source, "training_date_indices": training,
                      "measured_date_indices": tuple(int(x) for x in days)}
            if source is None:
                reports.append(RegimeFoldReport(**common, status="missing",
                               diagnostic="No supported total-return price field is available."))
                continue
            if len(training) < max(20, config.states * 5):
                reports.append(RegimeFoldReport(**common, status="insufficient",
                               diagnostic="Insufficient contiguous trailing market training features."))
                continue
            inference = tuple(range(end + 1, int(days[-1]) + 1))
            if not np.isfinite(features[list(inference)]).all():
                reports.append(RegimeFoldReport(**common, status="missing",
                               diagnostic="Missing market features between training and measured OOS dates."))
                continue
            try:
                model = FittedRegimeModel(config).fit(
                    tuple(panel.dates[i] for i in training), features[list(training)])
                probabilities = model.predict_proba(
                    tuple(panel.dates[i] for i in inference), features[list(inference)])
                probabilities = probabilities[days - end - 1]
                conditional = [conditional_ic(np.asarray(series)[selection], probabilities,
                                             allow_empty=True)
                               for series in (raw_ic, baseline_ic, augmented_ic)]
                reports.append(RegimeFoldReport(
                    **common, status="ok" if model.converged else "not_converged",
                    diagnostic="State IDs apply only within this fold; effective days are Kish ESS, not independent days.",
                    probabilities=tuple(tuple(float(p) for p in row) for row in probabilities),
                    conditional_rank_ic=conditional[0],
                    conditional_baseline_rank_ic=conditional[1],
                    conditional_augmented_rank_ic=conditional[2],
                    conditional_incremental_rank_ic=tuple(
                        {"state": a["state"], "rank_ic":
                         a["rank_ic"] - b["rank_ic"] if a["rank_ic"] is not None else None,
                         "effective_days": a["effective_days"]}
                        for a, b in zip(conditional[2], conditional[1], strict=True)),
                    converged=model.converged,
                    iterations=model.iterations,
                    iteration_limit_reached=model.iteration_limit_reached))
            except (ImportError, ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
                reports.append(RegimeFoldReport(**common, status="unavailable",
                               diagnostic=f"{type(exc).__name__}: {exc}"))
    return tuple(reports)


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
        self.iterations = 0
        self.iteration_limit_reached = False

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
            monitor = self._model.monitor_
            self.iterations = int(monitor.iter)
            history = tuple(monitor.history)
            # hmmlearn's flag also means "iteration budget exhausted" and can
            # accept a likelihood decrease. Report tolerance convergence only.
            self.converged = bool(len(history) >= 2 and
                                  0 <= history[-1] - history[-2] < monitor.tol)
            self._last_training_probability = self._forward(training, initial=None)[-1]
        else:
            self._model = GaussianMixture(n_components=self.config.states, covariance_type="diag",
                                          max_iter=self.config.max_iterations,
                                          random_state=self.config.seed, reg_covar=1e-4).fit(training)
            self.converged = bool(self._model.converged_)
            self.iterations = int(self._model.n_iter_)
        self.iteration_limit_reached = self.iterations >= self.config.max_iterations
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


def conditional_ic(daily_ic, probabilities, *, allow_empty=False):
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
    if (mass <= 0).any() and not allow_empty:
        raise ContractError("cannot estimate a state without probability mass")
    return tuple({"state": index, "rank_ic": float((probabilities[:, index] * values).sum() / mass[index]) if mass[index] > 0 else None,
                  "effective_days": float((probabilities[:, index] / mass[index]).dot(probabilities[:, index] / mass[index]) ** -1) if mass[index] > 0 else 0.0}
                 for index in range(probabilities.shape[1]))
