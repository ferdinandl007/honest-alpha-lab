"""Offline regression cases for the money-risk audit; never contact a broker."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from honest_alpha_lab.contracts import AgentKind, AlphaCandidate, AlphaStatus, ContractError
from honest_alpha_lab.ledger import AlphaRegistry, ImmutableLedger, TrialLedger


def candidate():
    return AlphaCandidate.new(AgentKind.SYMBOLIC_FACTOR, "fixture", {"nested": {"value": 1}}, "snapshot", ("lineage",))


@pytest.mark.parametrize("status", [s for s in AlphaStatus if s != AlphaStatus.PROPOSED])
def test_registry_rejects_pre_promoted_candidate(status):
    registry = AlphaRegistry()
    with pytest.raises(ContractError):
        registry.register(replace(candidate(), status=status))
    assert registry.all() == ()
    assert registry.events() == ()


def test_registry_owns_its_specification():
    registry = AlphaRegistry()
    original = candidate()
    registered = registry.register(original)
    original.specification["nested"]["value"] = 2
    registered.specification["nested"]["value"] = 3
    registry.get(original.candidate_id).specification["nested"]["value"] = 4
    registry.all()[0].specification["nested"]["value"] = 5
    assert registry.get(original.candidate_id).specification["nested"]["value"] == 1
    transitioned = registry.transition(original.candidate_id, AlphaStatus.SCREENED, "screener", "fixture")
    transitioned.specification["nested"]["value"] = 6
    assert registry.get(original.candidate_id).specification["nested"]["value"] == 1
    assert registry.verify()


def test_ledger_does_not_export_mutable_payload_references():
    ledger = ImmutableLedger()
    payload = {"nested": [1]}
    returned = ledger.append(payload)
    payload["nested"].append(2)
    returned.payload["nested"].append(3)
    ledger.entries()[0].payload["nested"].append(4)
    assert ledger.entries()[0].payload == {"nested": [1]}
    assert ledger.verify()


@pytest.mark.parametrize("terminal", ["complete", "fail"])
def test_trial_cannot_finish_twice_from_original_started_record(terminal):
    ledger = TrialLedger()
    started = ledger.start("candidate", AgentKind.SYMBOLIC_FACTOR, "snapshot", "policy")
    ledger.complete(started, {"rank_ic": .1})
    with pytest.raises(ContractError):
        if terminal == "complete":
            ledger.complete(started, {"rank_ic": .9})
        else:
            ledger.fail(started, "late failure")
    assert len(ledger.entries()) == 2


def test_trial_rejects_forged_started_identity():
    ledger = TrialLedger()
    started = ledger.start("candidate", AgentKind.SYMBOLIC_FACTOR, "snapshot", "policy")
    with pytest.raises(ContractError):
        ledger.complete(replace(started, candidate_id="different"), {})
    assert len(ledger.entries()) == 1


def test_snapshot_import_cli_forwards_explicit_feature_binding(tmp_path, monkeypatch):
    from honest_alpha_lab.cli import _parser
    from honest_alpha_lab.snapshots import ParquetSnapshot, SnapshotDeclaration

    declaration = tmp_path / "declaration.json"
    declared = {name: "fixture" for name in SnapshotDeclaration.__dataclass_fields__}
    declared.update(retrieved_at="2026-09-05T00:00:00Z", purpose="correctness_fixture")
    declaration.write_text(json.dumps(declared))
    binding = tmp_path / "bindings.json"
    binding.write_text(json.dumps({"event_signal": "a" * 64}))
    captured = {}
    def create(root, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(directory=tmp_path, snapshot_hash="fixture")
    monkeypatch.setattr(ParquetSnapshot, "create", create)
    args = _parser().parse_args(["import-snapshot", "--observations", "observations.parquet",
        "--universe", "universe.parquet", "--sessions", "sessions.parquet",
        "--declaration", str(declaration), "--feature-manifests", str(binding),
        "--feature-artifacts", str(tmp_path / "artifacts")])
    assert args.handler(args) == 0
    assert captured["feature_manifests"] == {"event_signal": "a" * 64}
    assert captured["feature_artifacts"] is not None
    args.feature_artifacts = None
    with pytest.raises(ContractError, match="feature-artifacts"):
        args.handler(args)
