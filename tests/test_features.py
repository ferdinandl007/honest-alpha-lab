"""Software fixtures for publication clocks; these are NOT alpha benchmarks."""
import json
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from io import BytesIO

import pandas as pd
import pytest

from honest_alpha_lab.artifacts import LocalArtifactStore
from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.dsl import Formula
from honest_alpha_lab.features import (
    EvidenceFeatureBuildTool,
    EvidenceSpan,
    ExposureVintage,
    FeaturePlan,
    FeatureSession,
    SourceVintage,
    build_from_payload,
    materialize_features,
)
from honest_alpha_lab.snapshots import ParquetSnapshot, SnapshotDeclaration


def clock(day, hour=20):
    return datetime(2024, 1, day, hour, tzinfo=UTC)


@pytest.fixture
def inputs(tmp_path):
    store = LocalArtifactStore(tmp_path / "sources")
    source_hash = store.put(b"Demand: 12")
    evidence_hash = store.put(b"Correctness fixture only: timestamp and mapping evidence")
    source = SourceVintage("dataset", "factory", "event-1", date(2024, 1, 2), 12,
                           clock(2), clock(2), clock(2), source_hash, evidence_hash,
                           (EvidenceSpan(8, 10, "12"),), "fixture-parser-v1", "deterministic")
    exposure = ExposureVintage("A", "factory", date(2024, 1, 1), None, clock(1), 0.5, evidence_hash)
    sessions = tuple(FeatureSession(date(2024, 1, day), clock(day, 21)) for day in (2, 3, 5, 8))
    plan = FeaturePlan("dataset", "demand", "event_sum", max_age_days=30)
    return store, source, exposure, sessions, plan


def build(inputs, *, sources=None, exposures=None, plan=None, sessions=None):
    store, source, exposure, calendar, default_plan = inputs
    return materialize_features(plan or default_plan,
                                (source,) if sources is None else sources,
                                (exposure,) if exposures is None else exposures,
                                calendar if sessions is None else sessions, store)


def test_default_clock_includes_collection_and_extraction(inputs):
    source = replace(inputs[1], retrieved_at=clock(3), extracted_at=clock(5))
    result = build(inputs, sources=(source,))
    assert [row.session.day for row in result.rows] == [5, 8]
    assert [row.value for row in result.rows] == [6, 6]
    replay = build(inputs, sources=(source,), plan=replace(inputs[4], clock="publication_replay"))
    assert [row.session.day for row in replay.rows] == [2, 3, 5, 8]


def test_after_close_and_exchange_session_lag(inputs):
    source = replace(inputs[1], published_at=clock(2, 22), retrieved_at=clock(2, 22), extracted_at=clock(2, 22))
    result = build(inputs, sources=(source,), plan=replace(inputs[4], lag_sessions=1))
    assert [row.session.day for row in result.rows] == [5, 8]


def test_lag_requires_untruncated_calendar(inputs):
    with pytest.raises(ContractError, match="warm-up"):
        build(inputs, plan=replace(inputs[4], lag_sessions=1))


def test_llm_replay_requires_explicit_diagnostic_opt_in(inputs):
    source = replace(inputs[1], extraction_kind="llm")
    plan = replace(inputs[4], clock="publication_replay")
    with pytest.raises(ContractError, match="opt-in"):
        build(inputs, sources=(source,), plan=plan)
    result = build(inputs, sources=(source,), plan=replace(plan, allow_retrospective_llm=True))
    hashes = result.publish(inputs[0])
    manifest = json.loads(inputs[0].get(hashes["manifest_hash"]))
    assert manifest["retrospective_replay"] is True
    assert manifest["financial_alpha_verified"] is False


def test_revision_and_withdrawal_do_not_rewrite_past(inputs):
    original = inputs[1]
    revision = replace(original, value=20, published_at=clock(3), retrieved_at=clock(3), extracted_at=clock(3))
    withdrawal = replace(original, value=None, published_at=clock(5), retrieved_at=clock(5), extracted_at=clock(5))
    initial = build(inputs)
    result = build(inputs, sources=(withdrawal, original, revision))
    assert [row.value for row in result.rows] == [6, 10]
    assert result.rows[0] == initial.rows[0]


def test_expiry_and_decay_use_observation_age_not_revision_age(inputs):
    source = replace(inputs[1], published_at=clock(3), retrieved_at=clock(3), extracted_at=clock(3))
    result = build(inputs, sources=(source,), plan=replace(inputs[4], max_age_days=3, half_life_days=1))
    assert [(row.session.day, row.value) for row in result.rows] == [(3, 3), (5, 0.75)]


def test_event_sum_distinct_ids_and_negative_exposure(inputs):
    extra = replace(inputs[1], observation_id="event-2", value=8)
    inverse = replace(inputs[2], asset="B", weight=-1)
    result = build(inputs, sources=(inputs[1], extra), exposures=(inputs[2], inverse))
    assert [(row.asset, row.value) for row in result.rows[:2]] == [("A", 10), ("B", -20)]


def test_proxy_requires_all_current_mapped_sources(inputs):
    exposure = replace(inputs[2], source_key="missing-factory")
    plan = replace(inputs[4], mode="latest_proxy")
    assert build(inputs, exposures=(inputs[2], exposure), plan=plan).rows == ()
    source = replace(inputs[1], source_key="missing-factory", value=2)
    assert build(inputs, sources=(inputs[1], source), exposures=(inputs[2], exposure), plan=plan).rows[0].value == 7


def test_proxy_latest_period_not_latest_publication(inputs):
    newer = replace(inputs[1], observation_id="period-2", observed_on=date(2024, 1, 3),
                    value=30, published_at=clock(3), retrieved_at=clock(3), extracted_at=clock(3))
    old_revision = replace(inputs[1], value=999, published_at=clock(5), retrieved_at=clock(5), extracted_at=clock(5))
    result = build(inputs, sources=(inputs[1], newer, old_revision), plan=replace(inputs[4], mode="latest_proxy"))
    assert [row.value for row in result.rows] == [6, 15, 15, 15]
    withdrawn = replace(newer, value=None, published_at=clock(5), retrieved_at=clock(5), extracted_at=clock(5))
    result = build(inputs, sources=(inputs[1], newer, withdrawn), plan=replace(inputs[4], mode="latest_proxy"))
    assert [row.value for row in result.rows] == [6, 15]


def test_mapping_revised_termination_does_not_revive_open_ended_version(inputs):
    termination = replace(inputs[2], valid_to=date(2024, 1, 2), available_at=clock(5))
    result = build(inputs, exposures=(inputs[2], termination), plan=replace(inputs[4], mode="latest_proxy"))
    assert [row.session.day for row in result.rows] == [2, 3]
    # Events remain attached to the owner on the event date, not the current owner.
    assert len(build(inputs, exposures=(inputs[2], termination)).rows) == 4


def test_future_mapping_revision_cannot_change_past(inputs):
    changed = replace(inputs[2], weight=0.25, available_at=clock(5))
    before = build(inputs)
    after = build(inputs, exposures=(inputs[2], changed))
    assert after.rows[:2] == before.rows[:2]
    assert [row.value for row in after.rows] == [6, 6, 3, 3]


def test_zero_mapping_is_explicit_removal(inputs):
    removal = replace(inputs[2], weight=0, available_at=clock(5))
    assert len(build(inputs, exposures=(inputs[2], removal)).rows) == 2


def test_no_event_is_missing_not_zero(inputs):
    result = build(inputs, sources=())
    assert result.rows == ()
    assert result.observations_frame().empty
    result.publish(inputs[0])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "12"])
def test_nonfinite_or_untyped_values_rejected(inputs, value):
    with pytest.raises(ContractError):
        replace(inputs[1], value=value)


@pytest.mark.parametrize("kind", ["duplicate_source", "duplicate_map", "bad_quote", "wrong_dataset", "changed_period", "naive_clock"])
def test_evidence_contract_failures(inputs, kind):
    with pytest.raises(ContractError):
        if kind == "duplicate_source":
            build(inputs, sources=(inputs[1], inputs[1]))
        elif kind == "duplicate_map":
            build(inputs, exposures=(inputs[2], inputs[2]))
        elif kind == "bad_quote":
            build(inputs, sources=(replace(inputs[1], spans=(EvidenceSpan(8, 10, "99"),)),))
        elif kind == "wrong_dataset":
            build(inputs, sources=(replace(inputs[1], dataset_id="another"),))
        elif kind == "changed_period":
            source = replace(inputs[1], observed_on=date(2024, 1, 1), published_at=clock(3), retrieved_at=clock(3), extracted_at=clock(3))
            build(inputs, sources=(inputs[1], source))
        else:
            replace(inputs[1], extracted_at=datetime(2024, 1, 2))  # noqa: DTZ001 -- deliberately invalid input


def test_ambiguous_proxy_periods_rejected(inputs):
    with pytest.raises(ContractError, match="ambiguous"):
        build(inputs, sources=(inputs[1], replace(inputs[1], observation_id="different")),
              plan=replace(inputs[4], mode="latest_proxy"))


def test_source_tampering_detected(inputs):
    (inputs[0].root / inputs[1].source_hash).write_bytes(b"Changed")
    with pytest.raises(ContractError, match="verification"):
        build(inputs)


def test_payload_cli_parquet_snapshot_and_dsl(tmp_path, inputs):
    result = build(inputs)
    request = json.loads(result.input_manifest_json)
    request.pop("schema_version")
    assert build_from_payload(request, inputs[0]).rows == result.rows
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    output = tmp_path / "output"
    process = subprocess.run([sys.executable, "-m", "honest_alpha_lab", "build-features",
                              "--request", str(request_path), "--source-artifacts", str(inputs[0].root),
                              "--output-artifacts", str(output)], capture_output=True, text=True, check=True)
    hashes = json.loads(process.stdout)
    artifacts = LocalArtifactStore(output)
    frame = pd.read_parquet(BytesIO(artifacts.get(hashes["observations_hash"])))
    observations = tmp_path / "observations.parquet"
    frame.to_parquet(observations, index=False)
    universe = tmp_path / "universe.parquet"
    pd.DataFrame([{"asset": "A", "valid_from": date(2024, 1, 1), "valid_to": None, "known_at": clock(1)}]).to_parquet(universe)
    sessions = tmp_path / "sessions.parquet"
    pd.DataFrame([{**asdict(item), "open_at": clock(item.session.day, 14)} for item in inputs[3]]).to_parquet(sessions)
    declaration = SnapshotDeclaration("fixture", "fixture", "fixture", "fixture", "fixture", "fixture",
                                      "not_prices", "not_applicable", "fixture", clock(8).isoformat(),
                                      purpose="correctness_fixture")
    snapshot = ParquetSnapshot.create(tmp_path / "snapshots", observations=observations,
                                      universe=universe, sessions=sessions, declaration=declaration,
                                      feature_manifests={"demand": hashes["manifest_hash"]}, feature_artifacts=artifacts)
    panel = snapshot.panel(fields=("demand",))
    assert Formula.parse("* demand 2").evaluate_panel(panel).ravel().tolist() == [12, 12, 12, 12]
    manifest = json.loads(artifacts.get(hashes["manifest_hash"]))
    assert manifest["row_count"] == 4
    assert manifest["row_provenance"][0]["contributions"]
    assert hashes["independently_approved"] is False


def test_payload_rejects_generated_commands_and_extra_fields(inputs):
    payload = json.loads(build(inputs).input_manifest_json)
    payload.pop("schema_version")
    payload["command"] = "arbitrary code"
    with pytest.raises(ContractError):
        build_from_payload(payload, inputs[0])


def test_agent_tool_produces_only_unapproved_artifacts(inputs, tmp_path):
    from honest_alpha_lab.contracts import AgentKind
    from honest_alpha_lab.tools import ToolName, ToolPolicy

    payload = json.loads(build(inputs).input_manifest_json)
    payload.pop("schema_version")
    result = EvidenceFeatureBuildTool(inputs[0], LocalArtifactStore(tmp_path / "outputs")).execute(payload)
    assert result.output["independently_approved"] is False
    assert result.output["row_count"] == 4
    for kind in (AgentKind.TEXT_EVENT, AgentKind.ALTERNATIVE_DATASET_CREATOR):
        assert ToolName.POINT_IN_TIME_FEATURE_BUILD in ToolPolicy().allowed_for(kind)
