import json

import pytest

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.evaluation import rank_ic
from honest_alpha_lab.research_vault import ResearchVault


def test_legacy_sealed_and_unknown_scope_not_returned_to_agents(tmp_path):
    vault = ResearchVault(tmp_path)
    for name, sealed in (("sealed", True), ("unknown", None), ("development", False)):
        record = {"evaluation": {"candidate_id": name, "sealed": sealed, "similarity": .2},
                  "reward": {"reward": 100}}
        (tmp_path / "evaluations" / f"{name}.json").write_text(json.dumps(record))
    assert [r["candidate_id"] for r in vault.shared_context()["already_evaluated"]] == ["development"]


def test_artifact_reads_verify_bytes(tmp_path):
    store = LocalArtifactStore(tmp_path)
    key = store.put(b"evidence")
    assert store.get(key) == b"evidence"
    (tmp_path / key).write_bytes(b"changed")
    with pytest.raises(ContractError, match="verification"):
        store.get(key)


def test_legacy_rank_ic_does_not_invent_information_from_ties():
    assert rank_ic([7, 7, 7], [1, 2, 3]) == 0
    assert rank_ic([1, 1, 2], [2, 2, 1]) == pytest.approx(-1)
