"""Snapshot integrity fixtures; not real-data alpha benchmarks."""
import json

import numpy as np
import pandas as pd
import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.snapshots import ParquetSnapshot, SnapshotDeclaration


def exports(tmp_path, *, naive=False, duplicate=False):
    sessions = pd.DataFrame({"session": ["2020-01-02", "2020-01-03", "2020-01-06"],
                             "open_at": ["2020-01-02T14:00:00Z", "2020-01-03T14:00:00Z", "2020-01-06T14:00:00Z"],
                             "decision_at": ["2020-01-02T21:00:00Z", "2020-01-03T21:00:00Z", "2020-01-06T21:00:00Z"]})
    observations = pd.DataFrame([
        ["2020-01-02", "A", "x", 1., "2020-01-02T20:00:00Z", "old"],
        ["2020-01-02", "A", "x", 99., "2020-01-04T20:00:00Z", "future-revision"],
        ["2020-01-03", "A", "x", 2., "2020-01-03T20:00:00Z", "current"],
        ["2020-01-06", "A", "x", 0., "2020-01-06T20:00:00Z", "terminal"],
        ["2020-01-02", "B", "x", 20., "2020-01-02T22:00:00Z", "too-late"],
    ], columns=["session", "asset", "field", "value", "available_at", "source_row_id"])
    if naive:
        observations.loc[0, "available_at"] = "2020-01-02T20:00:00"
    if duplicate:
        observations = pd.concat([observations, observations.iloc[[0]]], ignore_index=True)
    universe = pd.DataFrame([
        ["A", "2020-01-01", None, "2019-12-01T00:00:00Z"],
        ["A", "2020-01-01", "2020-01-03", "2020-01-03T20:00:00Z"],
        ["B", "2020-01-01", None, "2020-01-03T00:00:00Z"],
    ], columns=["asset", "valid_from", "valid_to", "known_at"])
    for name, data in (("sessions", sessions), ("observations", observations), ("universe", universe)):
        data.to_parquet(tmp_path / f"{name}.parquet", index=False)
    declaration = SnapshotDeclaration("fixture", "https://example.test", "test-only",
        "fixture://rights", "fixture://availability", "fixture://universe", "not-applicable",
        "explicit-test-terminal", "test-security-id", "2020-02-01T00:00:00+00:00",
        purpose="correctness_fixture")
    return ParquetSnapshot.create(tmp_path / "store", observations=tmp_path / "observations.parquet",
        universe=tmp_path / "universe.parquet", sessions=tmp_path / "sessions.parquet", declaration=declaration)


def test_publication_filter_and_dated_membership_versions(tmp_path):
    snapshot = exports(tmp_path)
    panel = snapshot.panel(fields=("x",))
    assert panel.fields["x"][0, 0] == 1  # later revision is never replayed early
    assert np.isnan(panel.fields["x"][0, 1])
    assert panel.eligible.tolist() == [[True, False], [True, True], [False, True]]
    assert panel.fields["x"][-1, 0] == 0  # keep terminal observation after universe exit
    assert snapshot.manifest["declaration"]["purpose"] == "correctness_fixture"


@pytest.mark.parametrize("options", [{"naive": True}, {"duplicate": True}])
def test_ambiguous_time_or_duplicates_rejected(tmp_path, options):
    with pytest.raises(ContractError):
        exports(tmp_path, **options).panel(fields=("x",))


def test_tampered_data_rejected_at_every_read(tmp_path):
    snapshot = exports(tmp_path)
    with (snapshot.directory / "observations.parquet").open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(ContractError, match="content changed"):
        snapshot.panel(fields=("x",))


def test_tampered_manifest_and_missing_provenance_rejected(tmp_path):
    snapshot = exports(tmp_path)
    manifest = dict(snapshot.manifest)
    manifest["declaration"] = {**manifest["declaration"], "provider": "other"}
    (snapshot.directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match="hash mismatch"):
        ParquetSnapshot(snapshot.directory)
    with pytest.raises(ContractError):
        SnapshotDeclaration(**{**snapshot.manifest["declaration"], "license_id": ""})


def test_unavailable_field_is_explicit_data_needed_error(tmp_path):
    with pytest.raises(ContractError, match="not available"):
        exports(tmp_path).panel(fields=("unknown",))
