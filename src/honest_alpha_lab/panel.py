"""Date-by-security computation with explicit missingness and eligibility.

NumPy is an analytics dependency. This module never executes generated Python.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

import numpy as np

from .contracts import ContractError, canonical_hash
from .dsl import Formula, Node


@dataclass(frozen=True)
class ResearchPanel:
    dates: tuple[date, ...]
    assets: tuple[str, ...]
    fields: Mapping[str, np.ndarray]
    eligible: np.ndarray

    def __post_init__(self):
        dates, assets = tuple(self.dates), tuple(self.assets)
        if not dates or any(type(day) is not date for day in dates):
            raise ContractError("panel dates must be nonempty trading-session dates")
        if dates != tuple(sorted(set(dates))):
            raise ContractError("panel dates must be unique and ascending")
        if not assets or len(set(assets)) != len(assets) or any(not asset for asset in assets):
            raise ContractError("panel assets must be unique nonempty identifiers")
        shape = (len(dates), len(assets))
        eligibility = np.asarray(self.eligible)
        if eligibility.shape != shape or eligibility.dtype.kind != "b":
            raise ContractError("eligibility must be a boolean date-by-security array")
        if not self.fields:
            raise ContractError("a panel requires at least one field")
        fields = {}
        for name, raw in self.fields.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ContractError("invalid DSL field name")
            array = np.array(raw, dtype=float, copy=True)
            if array.shape != shape or np.isinf(array).any():
                raise ContractError("fields must be aligned finite-or-missing panels")
            # Keep terminal observations even after universe exit for outcome
            # construction. Feature evaluation applies eligibility separately.
            # Immutable backing bytes prevent callers from re-enabling writes.
            fields[name] = np.frombuffer(array.tobytes(), dtype=float).reshape(shape)
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "assets", assets)
        object.__setattr__(self, "fields", MappingProxyType(fields))
        object.__setattr__(self, "eligible",
                           np.frombuffer(eligibility.tobytes(), dtype=bool).reshape(shape))

    @property
    def shape(self):
        return len(self.dates), len(self.assets)

    @property
    def content_hash(self):
        return canonical_hash({
            "dates": self.dates, "assets": self.assets,
            "eligible": hashlib.sha256(self.eligible.tobytes()).hexdigest(),
            "fields": {name: hashlib.sha256(values.tobytes()).hexdigest()
                       for name, values in self.fields.items()},
        })


def cross_sectional_rank(values: np.ndarray) -> np.ndarray:
    """Average tied ranks in [0,1], independently per date; NaN remains missing."""
    values = np.asarray(values, dtype=float)
    if values.ndim != 2:
        raise ContractError("rank requires date-by-security values")
    output = np.full_like(values, np.nan)
    for day, row in enumerate(values):
        indices = np.flatnonzero(np.isfinite(row))
        order = indices[np.argsort(row[indices], kind="stable")]
        start = 0
        while start < len(order):
            end = start + 1
            while end < len(order) and row[order[end]] == row[order[start]]:
                end += 1
            output[day, order[start:end]] = (start + end - 1) / (2 * max(len(order) - 1, 1))
            start = end
    return output


def evaluate_panel(formula: Formula, panel: ResearchPanel) -> np.ndarray:
    def run(node: Node):
        if node.kind == "constant":
            return np.full(panel.shape, float(node.value))
        if node.kind == "field":
            if node.value not in panel.fields:
                raise ContractError(f"unknown field {node.value!r}")
            result = panel.fields[node.value].copy()
            result[~panel.eligible] = np.nan
            return result
        values = [run(child) for child in node.children]
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            if node.kind == "operator":
                left, right = values
                if node.value == "+":
                    result = left + right
                elif node.value == "-":
                    result = left - right
                elif node.value == "*":
                    result = left * right
                else:
                    result = np.divide(left, right, out=np.full(panel.shape, np.nan), where=right != 0)
            elif node.value == "rank":
                result = cross_sectional_rank(values[0])
            elif node.value == "zscore":
                result = np.full(panel.shape, np.nan)
                for day, row in enumerate(values[0]):
                    valid = np.isfinite(row)
                    if valid.any():
                        result[day, valid] = (row[valid] - row[valid].mean()) / (row[valid].std() or 1)
            elif node.value in {"min", "max"}:
                # Missing either operand is not evidence for the other.
                result = (np.minimum if node.value == "min" else np.maximum).reduce(values)
            elif node.value == "winsorize":
                limit = float(node.children[1].value)
                result = np.clip(values[0], -limit, limit)
            else:
                window = int(node.children[1].value)
                result = np.full(panel.shape, np.nan)
                if node.value in {"lag", "ts_delta"}:
                    if window < panel.shape[0]:
                        result[window:] = values[0][:-window]
                        if node.value == "ts_delta":
                            result[window:] = values[0][window:] - result[window:]
                elif node.value == "ts_mean":
                    for day in range(window - 1, panel.shape[0]):
                        # Full windows only; missing sessions are not forward-filled.
                        result[day] = values[0][day - window + 1:day + 1].mean(axis=0)
                else:
                    raise ContractError(f"unsupported function {node.value!r}")
        result[~np.isfinite(result) | ~panel.eligible] = np.nan
        return result

    result = run(formula.root)
    result[~panel.eligible] = np.nan
    return result


@dataclass(frozen=True)
class ForwardLabels:
    horizon: int
    values: np.ndarray
    end_indices: tuple[int | None, ...]
    convention: str = "next_open_to_open_residual"

    def __post_init__(self):
        array = np.array(self.values, dtype=float, copy=True)
        if not isinstance(self.horizon, int) or self.horizon <= 0:
            raise ContractError("label horizon must be a positive integer")
        if array.ndim != 2 or len(self.end_indices) != len(array) or np.isinf(array).any():
            raise ContractError("invalid label alignment or values")
        for index, end in enumerate(self.end_indices):
            if end is None:
                if np.isfinite(array[index]).any():
                    raise ContractError("unknown label end cannot have observed outcomes")
            elif not isinstance(end, int) or end <= index or end >= len(array):
                raise ContractError("label end must follow its decision and exist in panel")
        object.__setattr__(self, "values",
                           np.frombuffer(array.tobytes(), dtype=float).reshape(array.shape))
        object.__setattr__(self, "end_indices", tuple(self.end_indices))

    @property
    def content_hash(self):
        return canonical_hash({"horizon": self.horizon, "shape": self.values.shape,
                               "values": hashlib.sha256(self.values.tobytes()).hexdigest(),
                               "ends": self.end_indices, "convention": self.convention})


def next_open_residual_labels(
    panel: ResearchPanel, horizon: int, beta: np.ndarray,
    stock_field: str = "total_return_open",
    sector_field: str = "sector_total_return_open",
    market_field: str = "market_total_return_open",
) -> ForwardLabels:
    """Outcome only, never an input feature.

    Betas must be estimated using information available by each decision. Sector
    returns are subtracted first, so beta is the incremental market exposure of
    the stock-minus-sector series, not the stock's full market beta.
    Prices must be consistently adjusted total-return indices supplied by the
    snapshot, including delisting proceeds. We do not invent missing outcomes.
    """
    if not isinstance(horizon, int) or horizon <= 0:
        raise ContractError("horizon must be a positive integer")
    beta = np.asarray(beta, dtype=float)
    if beta.shape != panel.shape or np.isinf(beta).any():
        raise ContractError("beta must be an aligned finite-or-missing panel")
    try:
        stock, sector, market = (panel.fields[name] for name in
                                 (stock_field, sector_field, market_field))
    except KeyError as exc:
        raise ContractError(f"missing total-return field {exc.args[0]}") from exc
    result = np.full(panel.shape, np.nan)
    ends = [None] * len(panel.dates)
    for t in range(len(panel.dates) - horizon - 1):
        start, end = t + 1, t + horizon + 1
        valid = panel.eligible[t] & np.isfinite(beta[t])
        for series in (stock, sector, market):
            valid &= np.isfinite(series[start]) & np.isfinite(series[end])
            valid &= (series[start] > 0) & (series[end] >= 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            values = ((stock[end] / stock[start] - 1)
                      - (sector[end] / sector[start] - 1)
                      - beta[t] * (market[end] / market[start] - 1))
        result[t, valid] = values[valid]
        ends[t] = end
    return ForwardLabels(horizon, result, tuple(ends))


def estimate_incremental_beta(panel: ResearchPanel, window: int = 252,
                              stock_field: str = "total_return_close",
                              sector_field: str = "sector_total_return_close",
                              market_field: str = "market_total_return_close") -> np.ndarray:
    """Trailing OLS of stock-minus-sector daily returns on market returns.

    Uses observations ending on the decision session only. No whole-sample fit,
    cross-security fill, or silent default beta. Requires a full window.
    """
    if not isinstance(window, int) or window < 2:
        raise ContractError("beta window must be an integer >= 2")
    try:
        stock, sector, market = (panel.fields[name] for name in
                                 (stock_field, sector_field, market_field))
    except KeyError as exc:
        raise ContractError(f"missing total-return field {exc.args[0]}") from exc
    def returns(values):
        out = np.full(panel.shape, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[1:] = np.where(values[:-1] > 0, values[1:] / values[:-1] - 1, np.nan)
        return out
    residual, market_returns = returns(stock) - returns(sector), returns(market)
    beta = np.full(panel.shape, np.nan)
    for t in range(window, len(panel.dates)):
        x, y = market_returns[t-window+1:t+1], residual[t-window+1:t+1]
        valid = np.isfinite(x).all(axis=0) & np.isfinite(y).all(axis=0)
        x_centered, y_centered = x - x.mean(axis=0), y - y.mean(axis=0)
        denominator = (x_centered ** 2).sum(axis=0)
        valid &= denominator > 1e-16
        beta[t, valid] = (x_centered[:, valid] * y_centered[:, valid]).sum(axis=0) / denominator[valid]
    return beta
