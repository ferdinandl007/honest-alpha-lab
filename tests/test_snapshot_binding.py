"""Content and timing fixtures, never evidence of financial alpha."""
import hashlib
import io
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from test_numerical_cli import make_numerical_snapshot

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import ContractError, canonical_hash
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.features import FeatureBuild, FeatureContribution, FeaturePlan, FeatureRow
from honest_alpha_lab.numerical import WalkForwardConfig, WalkForwardEvaluator
from honest_alpha_lab.panel import estimate_incremental_beta, next_open_residual_labels
from honest_alpha_lab.pipeline import FORMAT, NumericalPlan, development_feedback
from honest_alpha_lab.prediction_artifacts import store_predictions
from honest_alpha_lab.snapshots import ParquetSnapshot, SnapshotDeclaration


def bound_inputs(tmp_path):
    snapshot, _ = make_numerical_snapshot(tmp_path)
    observations = pd.read_parquet(snapshot.directory / "observations.parquet")
    feature_rows = observations[observations.field == "x"]
    plan = FeaturePlan("fixture", "x", "event_sum", 30,
                       clock="publication_replay", allow_retrospective_llm=True)
    # Explicit serialization fixture. These records test provenance transport,
    # not the economic meaning or historical reproducibility of the extraction.
    exposures = {asset: {"asset": asset, "source_key": asset, "valid_from": "2019-01-01",
        "valid_to": None, "available_at": "2019-01-01T00:00:00Z", "weight": 1,
        "evidence_hash": "b" * 64} for asset in feature_rows.asset.unique()}
    inputs = {"schema_version": 1, "plan": asdict(plan), "sources": [],
              "exposures": list(exposures.values()), "sessions": [
                  {"session": str(day), "decision_at": f"{day}T20:00:00Z"}
                  for day in feature_rows.session.unique()]}
    rows = []
    for r in feature_rows.itertuples():
        source = {"dataset_id": "fixture", "source_key": r.asset,
            "observation_id": r.source_row_id, "observed_on": str(r.session), "value": r.value,
            "published_at": r.available_at, "retrieved_at": "2020-12-01T00:00:00Z",
            "extracted_at": "2020-12-01T00:00:00Z", "extraction_kind": "llm",
            "source_hash": "a" * 64, "publication_evidence_hash": "b" * 64,
            "spans": [{"start": 0, "end": 1, "quote": "x"}], "extractor_id": "fixture"}
        inputs["sources"].append(source)
        contribution = FeatureContribution(canonical_hash(source), canonical_hash(exposures[r.asset]), r.value)
        rows.append(FeatureRow(pd.Timestamp(r.session).date(), r.asset, r.field, r.value,
                               pd.Timestamp(r.available_at).to_pydatetime(), r.source_row_id, (contribution,)))
    rows = tuple(rows)
    build = FeatureBuild(plan, canonical_hash(inputs), rows, json.dumps(inputs))
    artifacts = LocalArtifactStore(tmp_path / "feature-output")
    published = build.publish(artifacts)
    kwargs = {"observations": tmp_path / "observations.parquet",
              "universe": snapshot.directory / "universe.parquet",
              "sessions": snapshot.directory / "sessions.parquet",
              "declaration": SnapshotDeclaration(**snapshot.manifest["declaration"]),
              "feature_manifests": {"x": published["manifest_hash"]}, "feature_artifacts": artifacts}
    return snapshot, observations, artifacts, published, kwargs


def test_replay_binding_survives_merged_reserialized_rows_and_report_handoff(tmp_path):
    _, observations, artifacts, published, kwargs = bound_inputs(tmp_path)
    observations.iloc[::-1].to_parquet(kwargs["observations"], index=False)
    snapshot = ParquetSnapshot.create(tmp_path / "bound", **kwargs)
    panel = snapshot.panel(fields=tuple(observations.field.unique()))
    provenance = json.loads(panel.provenance_json)
    assert provenance["fields"]["x"] == {
        "kind": "materialized_feature", "manifest_hash": published["manifest_hash"],
        "clock": "publication_replay", "retrospective_llm": True}
    assert provenance["fields"]["total_return_open"]["clock"] == "unknown"
    labels = next_open_residual_labels(panel, 5, estimate_incremental_beta(panel, 5))
    report, scores = WalkForwardEvaluator(WalkForwardConfig(
        min_train_days=10, test_days=10, min_assets=4, bootstrap_samples=19,
        regime_configs=())).evaluate(Formula.parse("x"), panel, labels,
                                    snapshot_hash=snapshot.snapshot_hash)
    assert report.input_provenance == provenance
    handoff = store_predictions(snapshot, panel, scores, "fixture", artifacts)
    assert handoff["input_provenance"] == provenance
    assert not report.portfolio_performance_verified
    # Exercise the feedback reader without any external/database service.
    plan = NumericalPlan(snapshot.snapshot_hash, str(panel.dates[-1]), ("x",),
                         horizon=5, beta_window=5,
                         config=WalkForwardConfig(min_train_days=10, test_days=10,
                             min_assets=4, bootstrap_samples=19, regime_configs=()))
    report_payload = asdict(report)
    report_hash = artifacts.put(json.dumps(report_payload).encode())
    result = {"status": "evaluated", "scope": "development", "trial_id": "trial",
              "formula_hash": report.formula_hash, "report_artifact_hash": report_hash,
              "snapshot_purpose": "correctness_fixture"}
    records = [
        {"sequence": 1, "envelope": {"kind": "candidate_registered", "payload": {
            "candidate_hash": "candidate", "metadata": {}, "specification": {
                "expression": "x", "formula_hash": report.formula_hash}}}},
        {"sequence": 2, "envelope": {"kind": "trial_reserved", "payload": {
            "trial_id": "trial", "candidate_hash": "candidate"}}},
        {"sequence": 3, "envelope": {"kind": "trial_completed", "payload": {
            "trial_id": "trial", "result": result}}},
    ]
    run = {"policy": {"format": FORMAT, "plan": plan.payload()},
           "snapshot": {"scope": "development", "snapshot_hash": snapshot.snapshot_hash},
           "budget": {"max_runtime_seconds": 3600}, "created_at": datetime.now(UTC)}
    reader = SimpleNamespace(get_run=lambda _: run,
                             records=lambda _, after_sequence: records if not after_sequence else [])
    summary = development_feedback(reader, artifacts, "fixture")[0]
    assert summary["input_provenance"] == provenance
    assert summary["label_source_panel_hash"] == panel.content_hash
    for key in ("timing_contract", "label_source_panel_hash", "input_provenance", "schema_version"):
        report_payload.pop(key)
    result["report_artifact_hash"] = artifacts.put(json.dumps(report_payload).encode())
    run["policy"]["format"] = "numerical-job-v1"
    summary = development_feedback(reader, artifacts, "fixture")[0]
    assert summary["input_provenance"] == {"status": "legacy_unknown"}
    assert summary["timing_contract"] == "legacy_unknown"


@pytest.mark.parametrize("change", ["value", "missing", "duplicate", "clock", "field"])
def test_feature_binding_rejects_changed_or_incomplete_exports(tmp_path, change):
    _, observations, _, _, kwargs = bound_inputs(tmp_path)
    row = observations.index[observations.field == "x"][0]
    if change == "value":
        observations.loc[row, "value"] += 1
    elif change == "missing":
        observations = observations.drop(row)
    elif change == "duplicate":
        observations = pd.concat([observations, observations.loc[[row]]])
    elif change == "clock":
        observations.loc[row, "available_at"] = "2020-01-02T20:00:00Z"
    else:
        observations.loc[row, "field"] = "renamed"
    observations.to_parquet(kwargs["observations"], index=False)
    with pytest.raises(ContractError, match="feature rows"):
        ParquetSnapshot.create(tmp_path / "bound", **kwargs)


def test_bound_manifest_tampering_and_inconsistent_replay_flags_rejected(tmp_path):
    _, _, artifacts, published, kwargs = bound_inputs(tmp_path)
    manifest = json.loads(artifacts.get(published["manifest_hash"]))
    manifest["retrospective_replay"] = False
    bad_hash = artifacts.put(json.dumps(manifest).encode())
    with pytest.raises(ContractError, match="retrospective"):
        ParquetSnapshot.create(tmp_path / "bad", **{**kwargs, "feature_manifests": {"x": bad_hash}})
    snapshot = ParquetSnapshot.create(tmp_path / "bound", **kwargs)
    (snapshot.directory / "feature-artifacts" / published["manifest_hash"]).write_bytes(b"changed")
    with pytest.raises(ContractError, match="content changed"):
        snapshot.panel(fields=("x",))


def test_replay_cannot_be_relabeled_as_run_with_rehashed_manifest(tmp_path):
    _, _, artifacts, published, kwargs = bound_inputs(tmp_path)
    manifest = json.loads(artifacts.get(published["manifest_hash"]))
    manifest["plan"]["clock"] = manifest["inputs"]["plan"]["clock"] = "as_run"
    manifest["retrospective_replay"] = False
    manifest["input_hash"] = canonical_hash(manifest["inputs"])
    altered_hash = artifacts.put(json.dumps(manifest).encode())
    with pytest.raises(ContractError, match="dependency clocks"):
        ParquetSnapshot.create(tmp_path / "false-as-run", **{
            **kwargs, "feature_manifests": {"x": altered_hash}})


def test_export_without_dependency_references_cannot_claim_verified_clock(tmp_path):
    _, _, artifacts, published, kwargs = bound_inputs(tmp_path)
    manifest = json.loads(artifacts.get(published["manifest_hash"]))
    manifest["row_provenance"][0]["contributions"] = []
    altered_hash = artifacts.put(json.dumps(manifest).encode())
    with pytest.raises(ContractError, match="explicit dependencies"):
        ParquetSnapshot.create(tmp_path / "missing-dependencies", **{
            **kwargs, "feature_manifests": {"x": altered_hash}})


def test_v1_snapshot_is_inspectable_but_cannot_generate_executable_labels(tmp_path):
    original, _ = make_numerical_snapshot(tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    manifest = {"schema_version": 1, "declaration": original.manifest["declaration"], "files": {}}
    for name in ("observations.parquet", "universe.parquet", "sessions.parquet"):
        content = (original.directory / name).read_bytes()
        if name == "sessions.parquet":
            buffer = io.BytesIO()
            pd.read_parquet(io.BytesIO(content)).drop(columns="open_at").to_parquet(buffer, index=False)
            content = buffer.getvalue()
        (legacy / name).write_bytes(content)
        manifest["files"][name] = hashlib.sha256(content).hexdigest()
    manifest["snapshot_hash"] = canonical_hash(manifest)
    (legacy / "manifest.json").write_text(json.dumps(manifest))
    snapshot = ParquetSnapshot(legacy)
    panel = snapshot.panel(fields=("x", "total_return_open", "sector_total_return_open", "market_total_return_open"))
    assert np.isfinite(Formula.parse("x").evaluate_panel(panel)).any()
    assert json.loads(panel.provenance_json)["status"] == "legacy_unknown"
    with pytest.raises(ContractError, match="explicit"):
        next_open_residual_labels(panel, 5, np.zeros(panel.shape))
    with pytest.raises(ContractError, match="open_at"):
        ParquetSnapshot.create(tmp_path / "reimport", observations=legacy / "observations.parquet",
            universe=legacy / "universe.parquet", sessions=legacy / "sessions.parquet",
            declaration=SnapshotDeclaration(**manifest["declaration"]))


def test_snapshot_checks_clock_before_slicing_and_handoff_rejects_different_clock(tmp_path):
    snapshot, _ = make_numerical_snapshot(tmp_path)
    panel = snapshot.panel(fields=("x",))
    altered = replace(panel, decision_at=tuple(t + timedelta(minutes=1) for t in panel.decision_at))
    with pytest.raises(ContractError, match="calendar differs"):
        store_predictions(snapshot, altered, np.zeros(panel.shape), "fixture", LocalArtifactStore(tmp_path / "out"))
    sessions = pd.read_parquet(snapshot.directory / "sessions.parquet")
    sessions.loc[0, "decision_at"] = sessions.loc[1, "open_at"]
    sessions.to_parquet(tmp_path / "late-sessions.parquet", index=False)
    late = ParquetSnapshot.create(tmp_path / "late", observations=snapshot.directory / "observations.parquet",
        universe=snapshot.directory / "universe.parquet", sessions=tmp_path / "late-sessions.parquet",
        declaration=SnapshotDeclaration(**snapshot.manifest["declaration"]))
    with pytest.raises(ContractError, match="next open"):
        late.panel(fields=("x",), start=panel.dates[2])
