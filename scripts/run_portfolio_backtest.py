#!/usr/bin/env python3
"""Run a research-only backtest from a verified, point-in-time CSV snapshot."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from honest_alpha_lab.backtest_io import load_csv_dataset, load_training_returns
from honest_alpha_lab.portfolio import AllocationPolicy, PortfolioBacktester, TraditionalAllocator
from honest_alpha_lab.strategies import research_strategy_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only Honest Alpha Lab portfolio backtest")
    parser.add_argument("--bars", required=True, help="PIT OHLCV CSV")
    parser.add_argument("--signals", required=True, help="PIT score/event CSV")
    parser.add_argument("--training-returns", required=True, help="pre-test sleeve return CSV")
    parser.add_argument("--snapshot-hash", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allocation", choices=("minimum_variance", "inverse_volatility", "equal_weight"), default="minimum_variance")
    args = parser.parse_args()
    dataset = load_csv_dataset(args.bars, args.signals, args.snapshot_hash)
    allocation = TraditionalAllocator(AllocationPolicy(method=args.allocation)).allocate(load_training_returns(args.training_returns))
    result = PortfolioBacktester(research_strategy_catalog()).run(dataset, allocation)
    Path(args.output).write_text(json.dumps(asdict(result), default=str, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
