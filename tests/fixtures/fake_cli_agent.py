"""Tiny non-network CLI used to validate the file-agent protocol."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    prompt = sys.argv[-1]
    assert "output.schema.json" in prompt
    Path("result.json").write_text(
        json.dumps(
            {
                "alpha_candidates": [
                    {
                        "name": "cli:momentum",
                        "specification": {"dsl": "rank(momentum)"},
                        "lineage_ids": ["prices"],
                    }
                ],
                "output": {"agent": "fake-cli"},
                "usage": {"trials": 1, "agent_tokens": 12},
            }
        ),
        encoding="utf-8",
    )
    print(json.dumps({"type": "task.completed"}))


if __name__ == "__main__":
    main()
