"""Leakage-resistant splitting, sealed evaluation, and core alpha diagnostics."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from math import isclose, isfinite, sqrt
from numbers import Real
from pathlib import Path
import sqlite3
from statistics import mean
from typing import Callable, Iterator, Sequence

from .contracts import (
    AlphaCandidate,
    ContractError,
    EvaluationPolicy,
    InputSnapshot,
    canonical_hash,
)


def _require_finite(name: str, values: Sequence[float]) -> None:
    """Reject missing values, bools, coercible strings and nonfinite numbers."""
    try:
        valid = all(isinstance(value, Real) and not isinstance(value, bool) and isfinite(value)
                    for value in values)
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ContractError(f"{name} requires finite numerical evidence")


@dataclass(frozen=True, slots=True)
class Window:
    train_start: date
    train_end: date
    test_start: date
    test_end: date


class PurgedWalkForwardSplitter:
    def __init__(self, policy: EvaluationPolicy) -> None:
        self.policy = policy

    def split(self, dates: Sequence[date]) -> tuple[Window, ...]:
        ordered = tuple(sorted(set(dates)))
        windows: list[Window] = []
        train = self.policy.min_train_days
        test = self.policy.min_test_days
        step = test
        while (
            train + self.policy.purge_days + self.policy.embargo_days + test
            <= len(ordered)
        ):
            train_start, train_end = ordered[0], ordered[train - 1]
            test_start_index = train + self.policy.purge_days + self.policy.embargo_days
            test_start = ordered[test_start_index]
            test_end = ordered[test_start_index + test - 1]
            windows.append(Window(train_start, train_end, test_start, test_end))
            train += step
        return tuple(windows)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    candidate_id: str
    snapshot_hash: str
    policy_hash: str
    out_of_sample_rank_ic: float
    turnover: float
    similarity: float
    data_cost_usd: float
    fold_metrics: tuple[float, ...]
    sealed: bool = False

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.candidate_id, self.snapshot_hash, self.policy_hash)):
            raise ContractError("evaluation requires candidate, snapshot and policy identities")
        if type(self.sealed) is not bool:
            raise ContractError("sealed must be an explicit boolean")
        if not isinstance(self.fold_metrics, (tuple, list)) or not self.fold_metrics:
            raise ContractError("evaluation requires non-empty fold metrics")
        object.__setattr__(self, "fold_metrics", tuple(self.fold_metrics))
        _require_finite("evaluation", (self.out_of_sample_rank_ic, self.turnover,
                                      self.similarity, self.data_cost_usd, *self.fold_metrics))
        if not -1 <= self.out_of_sample_rank_ic <= 1 or any(
            not -1 <= value <= 1 for value in self.fold_metrics
        ):
            raise ContractError("rank IC evidence must be in [-1, 1]")
        if self.turnover < 0 or self.data_cost_usd < 0 or not 0 <= self.similarity <= 1:
            raise ContractError("evaluation turnover, cost or similarity is invalid")

    @property
    def result_hash(self) -> str:
        """Content identity, not an authentication credential."""
        return canonical_hash(self)

    @property
    def persistent(self) -> bool:
        return (
            len(self.fold_metrics) >= 2
            and sum(value > 0 for value in self.fold_metrics) / len(self.fold_metrics)
            >= 0.6
        )


@dataclass(frozen=True, slots=True)
class SealedTestConfig:
    snapshot_hash: str
    policy_hash: str
    test_start: date
    test_end: date
    config_hash: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.snapshot_hash, self.policy_hash)):
            raise ContractError("sealed config requires snapshot and policy identities")
        if (type(self.test_start) is not date or type(self.test_end) is not date
                or self.test_start > self.test_end):
            raise ContractError("sealed test dates must form a valid ordered interval")
        expected = canonical_hash({
            "snapshot_hash": self.snapshot_hash, "policy_hash": self.policy_hash,
            "test_start": self.test_start, "test_end": self.test_end,
        })
        if self.config_hash != expected:
            raise ContractError("sealed config hash does not match its contents")

    @classmethod
    def create(
        cls,
        snapshot: InputSnapshot,
        policy: EvaluationPolicy,
        test_start: date,
        test_end: date,
    ) -> "SealedTestConfig":
        config = {
            "snapshot_hash": snapshot.snapshot_hash,
            "policy_hash": canonical_hash(policy),
            "test_start": test_start,
            "test_end": test_end,
        }
        return cls(
            config["snapshot_hash"],
            config["policy_hash"],
            test_start,
            test_end,
            canonical_hash(config),
        )


class SealedTestGate:
    """Durably consume a holdout before validation or scorer execution.

    All workers MUST use the same persistent SQLite state_path on a filesystem
    supporting SQLite locks and durable writes. A committed reservation survives
    exceptions and process death. Changing candidate or policy cannot reset a
    consumed snapshot/date interval. New snapshots/intervals require external
    governance; this class cannot recognize overlapping or relabeled datasets.

    The evaluator must separately protect this database and test data with OS or
    service permissions. Python objects, hashes and caller-provided identities do
    not authenticate a caller or prevent database deletion/rollback/replacement.
    """

    def __init__(self, config: SealedTestConfig, *, state_path: str | Path) -> None:
        config.__post_init__()
        self._config = config
        if (not isinstance(state_path, (str, Path)) or not str(state_path).strip()
                or str(state_path) == ":memory:"):
            raise ContractError("sealed tests require a durable SQLite state_path")
        self._state_path = Path(state_path).expanduser().resolve()
        if not self._state_path.parent.is_dir() or self._state_path.is_dir():
            raise ContractError("sealed state_path must name a file in an existing directory")
        self._holdout_key = canonical_hash((config.snapshot_hash, config.test_start, config.test_end))
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sealed_attempts (
                    holdout_key TEXT PRIMARY KEY,
                    config_hash TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started', 'failed', 'completed')),
                    result_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self._state_path), timeout=30)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA fullfsync = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @property
    def config(self) -> SealedTestConfig:
        return self._config

    def run(
        self, candidate: AlphaCandidate, snapshot_hash: str, policy_hash: str,
        scorer: Callable[[AlphaCandidate, SealedTestConfig], EvaluationResult],
    ) -> EvaluationResult:
        config = self._config
        # Reserve first: even an invalid request or a failed scorer burns the attempt.
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO sealed_attempts "
                    "(holdout_key, config_hash, candidate_id, candidate_hash, snapshot_hash, policy_hash, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'started')",
                    (self._holdout_key, config.config_hash, "pending", "pending",
                     config.snapshot_hash, config.policy_hash),
                )
        except sqlite3.IntegrityError as exc:
            raise ContractError("sealed test is single-use; holdout already consumed") from exc
        try:
            if not isinstance(candidate, AlphaCandidate):
                raise ContractError("sealed test requires an AlphaCandidate")
            locked_candidate = deepcopy(candidate)
            candidate_hash = canonical_hash(locked_candidate)
            with self._connect() as connection:
                connection.execute(
                    "UPDATE sealed_attempts SET candidate_id = ?, candidate_hash = ? WHERE holdout_key = ?",
                    (locked_candidate.candidate_id, candidate_hash, self._holdout_key),
                )
            if (snapshot_hash != config.snapshot_hash or policy_hash != config.policy_hash
                    or locked_candidate.input_snapshot_hash != config.snapshot_hash):
                raise ContractError("sealed test inputs do not match the locked snapshot and policy")
            if getattr(locked_candidate.status, "value", None) not in {"validated", "accepted"}:
                raise ContractError("only numerically validated candidates may enter the sealed test")
            result = scorer(locked_candidate, config)
            if not isinstance(result, EvaluationResult) or result.sealed is not True:
                raise ContractError("sealed scorer must return a sealed EvaluationResult")
            result.__post_init__()
            if (result.candidate_id != locked_candidate.candidate_id
                    or result.snapshot_hash != config.snapshot_hash
                    or result.policy_hash != config.policy_hash):
                raise ContractError("sealed result candidate, snapshot or policy identity mismatch")
            if canonical_hash(locked_candidate) != candidate_hash or canonical_hash(candidate) != candidate_hash:
                raise ContractError("sealed candidate changed during evaluation")
            config.__post_init__()
            with self._connect() as connection:
                updated = connection.execute(
                    "UPDATE sealed_attempts SET status = 'completed', result_hash = ? "
                    "WHERE holdout_key = ? AND status = 'started' AND config_hash = ? AND candidate_hash = ?",
                    (result.result_hash, self._holdout_key, config.config_hash, candidate_hash),
                )
                if updated.rowcount != 1:
                    raise ContractError("sealed attempt state changed during evaluation")
            return result
        except BaseException as exc:
            try:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE sealed_attempts SET status = 'failed' WHERE holdout_key = ? AND status = 'started'",
                        (self._holdout_key,),
                    )
            except sqlite3.Error as state_error:
                # The committed 'started' reservation still forbids another attempt.
                exc.add_note(f"Could not record sealed failure: {state_error}")
            raise


def rank_ic(scores: Sequence[float], outcomes: Sequence[float]) -> float:
    if len(scores) != len(outcomes) or len(scores) < 2:
        raise ContractError(
            "rank_ic requires equally sized sequences with at least two observations"
        )
    _require_finite("rank IC inputs", (*scores, *outcomes))
    return _correlation(_ranks(scores), _ranks(outcomes))


def turnover(
    previous_weights: Sequence[float], current_weights: Sequence[float]
) -> float:
    if len(previous_weights) != len(current_weights):
        raise ContractError("turnover requires equal-length weight vectors")
    _require_finite("turnover weights", (*previous_weights, *current_weights))
    result = sum(abs(a - b) for a, b in zip(previous_weights, current_weights)) / 2.0
    _require_finite("turnover", (result,))
    return result


def half_life(decay_values: Sequence[float]) -> float | None:
    _require_finite("decay values", decay_values)
    if len(decay_values) < 2 or decay_values[0] <= 0:
        return None
    baseline = decay_values[0] / 2.0
    for index, value in enumerate(decay_values):
        if value <= baseline:
            return float(index)
    return None


def similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Absolute score correlation used to de-duplicate candidates."""
    if len(left) != len(right) or len(left) < 2:
        raise ContractError(
            "similarity requires equally sized sequences with at least two observations"
        )
    return abs(_correlation(left, right))


def stability_by_fold(fold_metrics: Sequence[float]) -> float:
    """Share of folds with positive performance, a simple persistence diagnostic."""
    if not fold_metrics:
        raise ContractError("stability requires at least one fold")
    _require_finite("fold metrics", fold_metrics)
    return sum(value > 0 for value in fold_metrics) / len(fold_metrics)


def trial_adjusted_pvalue(p_value: float, trial_count: int) -> float:
    """Bonferroni family-wise correction; use stronger estimators when scale permits."""
    _require_finite("p-value", (p_value,))
    if not 0 <= p_value <= 1 or type(trial_count) is not int or trial_count <= 0:
        raise ContractError(
            "p_value must be in [0, 1] and trial_count must be positive"
        )
    _require_finite("trial count", (trial_count,))
    return min(1.0, p_value * trial_count)


def conditional_performance(
    scores: Sequence[float],
    outcomes: Sequence[float],
    regime_probabilities: Sequence[Sequence[float]],
) -> tuple[float, ...]:
    """Return probability-weighted mean score * outcome per latent state.

    Scores are signed exposures, so this is conditional gross strategy payoff,
    not rank IC or a normalized portfolio return. Unsupported states fail closed.
    """
    if len(scores) != len(outcomes) or len(outcomes) != len(regime_probabilities):
        raise ContractError("conditional performance inputs must be aligned")
    if not regime_probabilities:
        raise ContractError("conditional performance requires regime probabilities")
    states = len(regime_probabilities[0])
    if states == 0 or any(len(row) != states for row in regime_probabilities):
        raise ContractError("regime probability rows must be rectangular")
    _require_finite("conditional scores and outcomes", (*scores, *outcomes))
    weighted_sum = [0.0] * states
    probability_sum = [0.0] * states
    for score, outcome, probabilities in zip(scores, outcomes, regime_probabilities):
        _require_finite("regime probabilities", probabilities)
        if any(not 0 <= probability <= 1 for probability in probabilities) or not isclose(
            sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ContractError("regime probability rows must be in [0, 1] and sum to one")
        for state, probability in enumerate(probabilities):
            weighted_sum[state] += probability * score * outcome
            probability_sum[state] += probability
    if any(value <= 0 for value in probability_sum):
        raise ContractError("each regime requires positive probability mass")
    result = tuple(weighted_sum[i] / probability_sum[i] for i in range(states))
    _require_finite("conditional performance", result)
    return result


def capacity_proxy(
    average_daily_dollar_volume: Sequence[float],
    turnover_rate: Sequence[float],
    participation: float = 0.1,
) -> float:
    """Conservative daily capacity proxy before a full market-impact simulation."""
    if (
        len(average_daily_dollar_volume) != len(turnover_rate)
        or not average_daily_dollar_volume
    ):
        raise ContractError("capacity inputs must be aligned and non-empty")
    _require_finite("capacity inputs", (*average_daily_dollar_volume, *turnover_rate, participation))
    if not 0 < participation <= 1:
        raise ContractError("participation must be in (0, 1]")
    if any(value < 0 for value in (*average_daily_dollar_volume, *turnover_rate)):
        raise ContractError("capacity volumes and turnover must be non-negative")
    capacities = [
        volume * participation / max(turnover, 1e-12)
        for volume, turnover in zip(average_daily_dollar_volume, turnover_rate)
    ]
    _require_finite("capacity estimates", capacities)
    return min(capacities)


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        for index in order[start:end]:
            result[index] = (start + end - 1) / 2
        start = end
    return result


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    _require_finite("correlation inputs", (*left, *right))
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_scale = sqrt(sum((a - left_mean) ** 2 for a in left))
    right_scale = sqrt(sum((b - right_mean) ** 2 for b in right))
    return numerator / (left_scale * right_scale) if left_scale and right_scale else 0.0
