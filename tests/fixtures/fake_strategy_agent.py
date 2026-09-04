"""Offline subprocess fixture exercising the real CLI worker protocol."""
import json
from pathlib import Path

blueprint = {"strategy_id": "blend", "name": "Equal blend", "signal_ids": ["a", "b"],
             "combination": "linear_equal", "horizon_days": 20, "side": "long_only",
             "rebalance_days": 1, "execution": "next_bar_limit", "rationale": "Synthetic fixture"}
Path("result.json").write_text(json.dumps({
    "alpha_candidates": [], "output": {"summary": "Daily proposal", "notes": [json.dumps(blueprint)]},
    "usage": {"trials": 1, "runtime_seconds": 0, "data_cost_usd": 0, "agent_tokens": 10},
}))
