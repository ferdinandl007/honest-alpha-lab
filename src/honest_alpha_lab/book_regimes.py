"""Describe book performance under pre-day probabilistic market states."""
from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

import numpy as np

from .contracts import ContractError
from .regimes import FittedRegimeModel, RegimeConfig


def book_regime_analysis(content, comparisons, training_end, *, window=20, states=3):
    """Market CSV: day,close,available_at, using a consistent market index.

    Each book return is conditioned on the preceding supplied market session's
    filtered probabilities, available before that return date starts (UTC).
    Training ends before the internal portfolio test. No state-specific weights.
    """
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    if not rows or not {"day", "close", "available_at"} <= rows[0].keys():
        raise ContractError("market history needs day,close,available_at")
    dates = tuple(date.fromisoformat(row["day"]) for row in rows)
    if dates != tuple(sorted(set(dates))):
        raise ContractError("market history must have unique ordered dates")
    prices = np.array([float(row["close"]) for row in rows])
    clocks = tuple(datetime.fromisoformat(row["available_at"]) for row in rows)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ContractError("market index closes must be finite positive values")
    if any(stamp.utcoffset() is None or stamp.date() < day for stamp, day in zip(clocks, dates)):
        raise ContractError("market publication clocks must be aware and cannot precede sessions")
    returns = np.r_[np.nan, prices[1:] / prices[:-1] - 1]
    features = np.full((len(dates), 3), np.nan)
    for i in range(window, len(dates)):
        trailing = returns[i - window + 1:i + 1]
        features[i] = returns[i], trailing.mean(), trailing.std()
    # Feature clocks include every constituent; old late releases cannot sneak in.
    feature_clocks = [max(clocks[max(0, i-window):i+1]) for i in range(len(dates))]
    end = date.fromisoformat(training_end)
    test_start = min(date.fromisoformat(item["day"]) for comparison in comparisons.values()
                     for item in comparison["metrics"]["daily"])
    cutoff = datetime.combine(test_start, datetime.min.time(), UTC)
    train = [i for i, day in enumerate(dates) if day <= end and i >= window]
    if len(train) < max(20, states * 5):
        return {"status": "insufficient", "reason": "need market warmup plus 20 training observations", "methods": []}
    if any(feature_clocks[i] >= cutoff for i in train):
        raise ContractError("market training observations were unavailable before portfolio test")
    stop = train[-1]
    future = tuple(range(stop + 1, len(dates)))
    if not future:
        return {"status": "insufficient", "reason": "no out-of-sample market sessions", "methods": []}
    results = []
    for method in ("hmm", "gaussian_mixture"):
        model = FittedRegimeModel(RegimeConfig(method=method, states=states))
        try:
            model.fit(tuple(dates[i] for i in train), features[train])
            probabilities = model.predict_proba(tuple(dates[i] for i in future), features[list(future)])
        except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
            results.append({"method": method, "status": "unavailable", "error_type": type(exc).__name__})
            continue
        lookup = {dates[i]: index for index, i in enumerate(future)}
        breakdown = {}
        for name, comparison in comparisons.items():
            paired = []
            missing = []
            for observation in comparison["metrics"]["daily"]:
                day = date.fromisoformat(observation["day"])
                # A market row for the return date confirms the calendar alignment.
                if day not in dates:
                    missing.append(day.isoformat())
                    continue
                i = dates.index(day) - 1
                if i <= stop or feature_clocks[i] >= datetime.combine(day, datetime.min.time(), UTC):
                    missing.append(day.isoformat())
                    continue
                p = probabilities[lookup[dates[i]]]
                paired.append((day.isoformat(), float(observation["return"]), p))
            statistics = []
            if paired:
                values = np.array([item[1] for item in paired])
                ps = np.array([item[2] for item in paired])
                for state in range(states):
                    w = ps[:, state]
                    if w.sum() <= 0:
                        statistics.append({"state": state, "effective_days": 0, "mean_daily_return": None})
                        continue
                    w = w / w.sum()
                    avg = float(w @ values)
                    statistics.append({"state": state, "effective_days": float(1 / (w @ w)),
                        "mean_daily_return": avg, "daily_volatility": float(np.sqrt(w @ (values-avg)**2)),
                        "loss_probability": float(w @ (values < 0)),
                        "mean_negative_return_contribution": float(w @ np.minimum(values, 0))})
            breakdown[name] = {"states": statistics, "missing_dates": missing,
                "observations": [{"day": day, "return": value, "probabilities": p.tolist()} for day, value, p in paired]}
        results.append({"method": method, "status": "ok" if model.converged else "not_converged",
                        "training_end": dates[stop].isoformat(), "converged": model.converged,
                        "comparisons": breakdown})
    return {"status": "evaluated", "conditioning": "prior_market_session_available_before_return_date_UTC",
            "note": "Descriptive weighted moments; states are method-local; ESS is not independent days. No significance or allocation claim.",
            "methods": results}
