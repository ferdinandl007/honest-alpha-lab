"""Evidence contracts and durable holdout isolation; fixtures are not alpha evidence."""

from dataclasses import fields, replace
from datetime import date, datetime, timezone
import multiprocessing
import os
import sqlite3

import pytest

from honest_alpha_lab.contracts import (
    AgentKind, AlphaCandidate, AlphaStatus, ContractError, EvaluationPolicy,
    InputSnapshot, canonical_hash,
)
from honest_alpha_lab.evaluation import (
    EvaluationResult, SealedTestConfig, SealedTestGate, capacity_proxy,
    conditional_performance, rank_ic, similarity, stability_by_fold,
    trial_adjusted_pvalue, turnover,
)
from honest_alpha_lab.reward import ResearchRewardHarness, ResearchRewardPolicy, RewardEvidence


def _evaluation(**changes):
    values = dict(
        candidate_id="candidate", snapshot_hash="snapshot", policy_hash="policy",
        out_of_sample_rank_ic=0.02, turnover=0.3, similarity=0.2,
        data_cost_usd=25.0, fold_metrics=(0.01, 0.02, -0.01, 0.03), sealed=False,
    )
    values.update(changes)
    return EvaluationResult(**values)


def _evidence(**changes):
    values = dict(
        candidate_id="candidate", agent_kind=AgentKind.SYMBOLIC_FACTOR,
        evaluation=_evaluation(), raw_p_value=0.001, trial_count=10, complexity=1.0,
        capacity_proxy_usd=2_000_000.0, point_in_time_verified=True,
        lineage_verified=True, incremental_rank_ic=0.008, regime_rank_ics=(0.01, 0.02),
    )
    values.update(changes)
    return RewardEvidence(**values)


def _sealed_inputs():
    snapshot = InputSnapshot(
        ("dataset",), "universe", "code", datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    config = SealedTestConfig.create(
        snapshot, EvaluationPolicy(), date(2026, 1, 1), date(2026, 3, 31)
    )
    candidate = AlphaCandidate(
        "candidate", AgentKind.SYMBOLIC_FACTOR, "frozen factor", {"dsl": "rank(price)"},
        snapshot.snapshot_hash, ("prices",), datetime(2026, 1, 1, tzinfo=timezone.utc),
        AlphaStatus.VALIDATED,
    )
    return config, candidate


def _sealed_score(candidate, config):
    return _evaluation(candidate_id=candidate.candidate_id, snapshot_hash=config.snapshot_hash,
                       policy_hash=config.policy_hash, sealed=True)


def _run(gate, candidate, scorer=_sealed_score):
    return gate.run(candidate, gate.config.snapshot_hash, gate.config.policy_hash, scorer)


def _state(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return dict(connection.execute("SELECT * FROM sealed_attempts").fetchone())
    finally:
        connection.close()


def test_missing_incremental_evidence_never_earns_raw_ic_credit():
    result = ResearchRewardHarness().score(_evidence(incremental_rank_ic=None))
    assert result.incremental_rank_ic is None
    assert result.components["incremental_information"] == 0
    assert result.components["oos_information"] > 0  # diagnostic only
    assert not result.gates["incremental_evidence"]
    assert not result.eligible_for_registry_validation
    assert result.reward == 0


def test_zero_and_negative_incremental_evidence_are_preserved():
    harness = ResearchRewardHarness()
    for value in (0.0, -0.01, 0.008):
        result = harness.score(_evidence(incremental_rank_ic=value))
        assert result.incremental_rank_ic == value
        assert result.gates["incremental_evidence"]
        assert result.components["incremental_information"] == value * 100


@pytest.mark.parametrize("value", [0.0, -0.01])
def test_nonincremental_candidates_do_not_pass_validation(value):
    result = ResearchRewardHarness().score(_evidence(incremental_rank_ic=value))
    assert not result.eligible_for_registry_validation
    assert not result.gates["incremental_information"]


def test_missing_market_state_evidence_is_not_robustness():
    result = ResearchRewardHarness().score(_evidence(regime_rank_ics=()))
    assert not result.eligible_for_registry_validation
    assert not result.gates["market_state_evidence"]
    assert result.reward == 0


@pytest.mark.parametrize("field", ["point_in_time_verified", "lineage_verified"])
@pytest.mark.parametrize("value", [None, False, 1, "true", float("nan"), float("inf")])
def test_integrity_requires_explicit_true(field, value):
    result = ResearchRewardHarness().score(_evidence(**{field: value}))
    assert result.gates[field] is False
    assert result.reward == 0
    assert not result.eligible_for_registry_validation


@pytest.mark.parametrize("kind", [AgentKind.ALTERNATIVE_DATA, AgentKind.ALTERNATIVE_DATASET_CREATOR])
@pytest.mark.parametrize("value", [None, False, 1, "verified", float("nan"), float("inf"), True])
def test_both_alternative_paths_require_explicit_mechanism_evidence(kind, value):
    result = ResearchRewardHarness().score(_evidence(agent_kind=kind, semantic_chain_verified=value))
    assert result.gates["semantic_chain"] is (value is True)
    assert result.eligible_for_registry_validation is (value is True)


def test_omitted_mechanism_evidence_fails_closed():
    result = ResearchRewardHarness().score(_evidence(agent_kind=AgentKind.ALTERNATIVE_DATASET_CREATOR))
    assert not result.gates["semantic_chain"]
    assert result.reward == 0


@pytest.mark.parametrize("field", ["raw_p_value", "complexity", "capacity_proxy_usd", "incremental_rank_ic"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "0.1", True])
def test_nonfinite_or_untyped_reward_evidence_is_rejected(field, value):
    with pytest.raises(ContractError):
        _evidence(**{field: value})


@pytest.mark.parametrize("field", ["raw_p_value", "complexity", "capacity_proxy_usd"])
def test_required_reward_numbers_cannot_be_missing(field):
    with pytest.raises(ContractError):
        _evidence(**{field: None})


@pytest.mark.parametrize("value", [None, 0, -1, 1.5, True, float("nan"), float("inf"), 10**400])
def test_trial_count_requires_a_finite_positive_integer(value):
    with pytest.raises(ContractError):
        _evidence(trial_count=value)


@pytest.mark.parametrize("field", ["out_of_sample_rank_ic", "turnover", "similarity", "data_cost_usd"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -float("inf"), True])
def test_evaluation_requires_finite_numbers(field, value):
    with pytest.raises(ContractError):
        _evaluation(**{field: value})


@pytest.mark.parametrize("changes", [
    {"out_of_sample_rank_ic": 1.1}, {"turnover": -0.1}, {"similarity": -0.1},
    {"similarity": 1.1}, {"data_cost_usd": -1}, {"fold_metrics": ()},
    {"fold_metrics": None}, {"fold_metrics": (0.1, float("nan"))},
    {"fold_metrics": (0.1, float("inf"))}, {"fold_metrics": (1.1,)},
    {"sealed": 1}, {"sealed": None}, {"candidate_id": ""},
    {"snapshot_hash": None}, {"policy_hash": " "},
])
def test_invalid_evaluation_contracts(changes):
    with pytest.raises(ContractError):
        _evaluation(**changes)


@pytest.mark.parametrize("changes", [
    {"regime_rank_ics": (float("nan"),)}, {"regime_rank_ics": (float("inf"),)},
    {"regime_rank_ics": (None,)}, {"regime_rank_ics": (1.1,)},
    {"incremental_rank_ic": -2.1}, {"complexity": -1},
    {"capacity_proxy_usd": -1}, {"candidate_id": "other"}, {"agent_kind": "unknown"},
])
def test_invalid_reward_contracts(changes):
    with pytest.raises(ContractError):
        _evidence(**changes)


@pytest.mark.parametrize("field", [field.name for field in fields(ResearchRewardPolicy)])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), None, -1.0])
def test_policy_cannot_disable_gates_with_invalid_numbers(field, value):
    with pytest.raises(ContractError):
        ResearchRewardPolicy(**{field: value})


def test_arithmetic_overflow_cannot_produce_a_reward():
    harness = ResearchRewardHarness(ResearchRewardPolicy(complexity_weight=1e308))
    with pytest.raises(ContractError):
        harness.score(_evidence(complexity=1e308))


def test_development_reward_and_leaderboard_are_isolated_from_sealed_outcomes():
    harness = ResearchRewardHarness()
    development = _evidence()
    result = harness.score(development)
    assert result.reward > 0
    assert result.eligible_for_registry_validation
    assert not result.eligible_for_registry_acceptance
    failed = _evidence(point_in_time_verified=False)
    assert harness.leaderboard((failed, development))[0] == result
    for ic in (-0.9, 0.02, 0.9):
        sealed = _evidence(evaluation=_evaluation(sealed=True, out_of_sample_rank_ic=ic))
        with pytest.raises(ContractError, match="DEVELOPMENT"):
            harness.score(sealed)
        with pytest.raises(ContractError, match="DEVELOPMENT"):
            harness.leaderboard((development, sealed))
    assert harness.score(development) == result


def test_sequence_inputs_are_copied_to_keep_evidence_stable():
    folds, regimes = [0.01, 0.02], [0.01]
    evidence = _evidence(evaluation=_evaluation(fold_metrics=folds), regime_rank_ics=regimes)
    folds[0] = float("nan")
    regimes[0] = float("nan")
    assert evidence.evaluation.fold_metrics == (0.01, 0.02)
    assert evidence.regime_rank_ics == (0.01,)


def test_sealed_state_requires_an_explicit_durable_location(tmp_path):
    config, _ = _sealed_inputs()
    with pytest.raises(TypeError):
        SealedTestGate(config)
    for path in (None, ":memory:", "", tmp_path, tmp_path / "missing" / "state.sqlite"):
        with pytest.raises(ContractError):
            SealedTestGate(config, state_path=path)


@pytest.mark.parametrize("changes", [
    {"config_hash": "forged"}, {"policy_hash": "changed"}, {"snapshot_hash": "changed"},
    {"test_start": date(2027, 1, 1)}, {"test_end": None},
])
def test_config_identity_is_checked(changes):
    config, _ = _sealed_inputs()
    with pytest.raises(ContractError):
        replace(config, **changes)


def test_unavailable_state_store_never_calls_scorer(tmp_path, monkeypatch):
    config, candidate = _sealed_inputs()
    gate = SealedTestGate(config, state_path=tmp_path / "sealed.sqlite")
    def unavailable():
        raise sqlite3.OperationalError("storage unavailable")
    monkeypatch.setattr(gate, "_connect", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="storage unavailable"):
        _run(gate, candidate, lambda *_: pytest.fail("storage must commit before scoring"))


def test_failure_recording_error_preserves_reservation_and_original_error(tmp_path, monkeypatch):
    config, candidate = _sealed_inputs()
    path = tmp_path / "sealed.sqlite"
    gate = SealedTestGate(config, state_path=path)
    def unavailable():
        raise sqlite3.OperationalError("storage unavailable")
    def scorer(*_):
        monkeypatch.setattr(gate, "_connect", unavailable)
        raise RuntimeError("original scorer failure")
    with pytest.raises(RuntimeError, match="original scorer failure") as error:
        _run(gate, candidate, scorer)
    assert "Could not record sealed failure" in error.value.__notes__[0]
    assert _state(path)["status"] == "started"
    with pytest.raises(ContractError, match="single-use"):
        _run(SealedTestGate(config, state_path=path), candidate)


def test_completed_attempt_binds_content_and_survives_new_instances(tmp_path):
    config, candidate = _sealed_inputs()
    path = tmp_path / "sealed.sqlite"
    first = SealedTestGate(config, state_path=path)
    other = SealedTestGate(config, state_path=path)
    result = _run(first, candidate)
    state = _state(path)
    assert state["status"] == "completed"
    assert state["candidate_id"] == candidate.candidate_id
    assert state["candidate_hash"] == canonical_hash(candidate)
    assert state["snapshot_hash"] == config.snapshot_hash
    assert state["policy_hash"] == config.policy_hash
    assert state["config_hash"] == config.config_hash
    assert state["result_hash"] == result.result_hash
    for gate in (first, other, SealedTestGate(config, state_path=path)):
        with pytest.raises(ContractError, match="single-use"):
            _run(gate, candidate, lambda *_: pytest.fail("scorer must not run twice"))


@pytest.mark.parametrize("field", ["candidate_id", "snapshot_hash", "policy_hash"])
def test_mismatched_result_is_rejected_and_burns_attempt(tmp_path, field):
    config, candidate = _sealed_inputs()
    path = tmp_path / "sealed.sqlite"
    gate = SealedTestGate(config, state_path=path)
    def scorer(current, locked):
        return replace(_sealed_score(current, locked), **{field: "wrong"})
    with pytest.raises(ContractError, match="identity mismatch"):
        _run(gate, candidate, scorer)
    assert _state(path)["status"] == "failed"
    assert _state(path)["result_hash"] is None
    with pytest.raises(ContractError, match="single-use"):
        _run(SealedTestGate(config, state_path=path), candidate)


@pytest.mark.parametrize("failure", ["exception", "interrupt", "unsealed", "wrong_type", "nonfinite", "mutation"])
def test_scorer_failures_and_mutations_burn_attempt(tmp_path, failure):
    config, candidate = _sealed_inputs()
    path = tmp_path / "sealed.sqlite"
    gate = SealedTestGate(config, state_path=path)
    def scorer(current, locked):
        if failure == "exception":
            raise RuntimeError("scoring failed")
        if failure == "interrupt":
            raise KeyboardInterrupt()
        if failure == "wrong_type":
            return {"sealed": True}
        if failure == "mutation":
            current.specification["dsl"] = "different formula"
        result = _sealed_score(current, locked)
        if failure == "unsealed":
            return replace(result, sealed=False)
        if failure == "nonfinite":
            # Exercise validation at the boundary even if a caller bypasses construction.
            object.__setattr__(result, "turnover", float("nan"))
        return result
    with pytest.raises((ContractError, RuntimeError, KeyboardInterrupt)):
        _run(gate, candidate, scorer)
    assert candidate.specification == {"dsl": "rank(price)"}
    assert _state(path)["status"] == "failed"
    with pytest.raises(ContractError, match="single-use"):
        _run(SealedTestGate(config, state_path=path), candidate)


@pytest.mark.parametrize("failure", ["snapshot", "policy", "candidate_snapshot", "status", "candidate_type"])
def test_invalid_entry_also_burns_attempt_without_calling_scorer(tmp_path, failure):
    config, candidate = _sealed_inputs()
    path = tmp_path / "sealed.sqlite"
    gate = SealedTestGate(config, state_path=path)
    current = candidate
    if failure == "candidate_snapshot":
        current = replace(candidate, input_snapshot_hash="wrong")
    elif failure == "status":
        current = replace(candidate, status=AlphaStatus.PROPOSED)
    elif failure == "candidate_type":
        current = None
    with pytest.raises(ContractError):
        gate.run(current, "wrong" if failure == "snapshot" else config.snapshot_hash,
                 "wrong" if failure == "policy" else config.policy_hash,
                 lambda *_: pytest.fail("invalid inputs must never reach scorer"))
    assert _state(path)["status"] == "failed"
    with pytest.raises(ContractError, match="single-use"):
        _run(SealedTestGate(config, state_path=path), candidate)


def test_changing_candidate_or_policy_does_not_reset_holdout(tmp_path):
    config, candidate = _sealed_inputs()
    path = tmp_path / "sealed.sqlite"
    _run(SealedTestGate(config, state_path=path), candidate)
    changed_policy = "another-policy"
    changed = replace(config, policy_hash=changed_policy, config_hash=canonical_hash({
        "snapshot_hash": config.snapshot_hash, "policy_hash": changed_policy,
        "test_start": config.test_start, "test_end": config.test_end,
    }))
    with pytest.raises(ContractError, match="single-use"):
        _run(SealedTestGate(changed, state_path=path), replace(candidate, candidate_id="other"))


def _race_worker(path, ready, start, results):
    config, candidate = _sealed_inputs()
    called = False
    try:
        gate = SealedTestGate(config, state_path=path)
        ready.put(True)
        if not start.wait(15):
            raise RuntimeError("start barrier timed out")
        def scorer(current, locked):
            nonlocal called
            called = True
            return _sealed_score(current, locked)
        _run(gate, candidate, scorer)
        results.put(("completed", called))
    except ContractError:
        results.put(("denied", called))
    except Exception as exc:
        results.put((str(exc), called))


def test_independent_processes_atomically_allow_only_one_scorer(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    path = tmp_path / "sealed.sqlite"
    workers = [context.Process(target=_race_worker, args=(path, ready, start, results)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=20)
        start.set()
        outcomes = [results.get(timeout=20) for _ in workers]
        assert outcomes.count(("completed", True)) == 1
        assert outcomes.count(("denied", False)) == 3
        assert _state(path)["status"] == "completed"
    finally:
        start.set()
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        ready.close()
        results.close()


def _crash_worker(path):
    config, candidate = _sealed_inputs()
    def scorer(*_):
        os._exit(23)
    _run(SealedTestGate(config, state_path=path), candidate, scorer)


def test_process_death_after_reservation_cannot_reopen_holdout(tmp_path):
    path = tmp_path / "sealed.sqlite"
    worker = multiprocessing.get_context("spawn").Process(target=_crash_worker, args=(path,))
    worker.start()
    try:
        worker.join(timeout=20)
        assert worker.exitcode == 23
        assert _state(path)["status"] == "started"
        config, candidate = _sealed_inputs()
        with pytest.raises(ContractError, match="single-use"):
            _run(SealedTestGate(config, state_path=path), candidate)
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)


def test_conditional_performance_uses_signed_scores():
    probabilities = ((0.8, 0.2), (0.2, 0.8))
    forward = conditional_performance((1, 2), (0.1, 0.3), probabilities)
    reverse = conditional_performance((-1, -2), (0.1, 0.3), probabilities)
    assert forward == pytest.approx((0.20, 0.50))
    assert reverse == pytest.approx(tuple(-value for value in forward))
    assert conditional_performance((0, 0), (0.1, 0.3), probabilities) == (0, 0)


@pytest.mark.parametrize("scores,outcomes,probabilities", [
    ([], [], []), ([1], [0.1, 0.2], [[1]]), ([1], [0.1], [[]]),
    ([1, 2], [0.1, 0.2], [[1], [0.5, 0.5]]),
    ([float("nan")], [0.1], [[1]]), ([1], [float("inf")], [[1]]),
    ([1], [0.1], [[float("nan")]]), ([1], [0.1], [[float("inf")]]),
    ([1], [0.1], [[-0.1, 1.1]]), ([1], [0.1], [[0.2, 0.2]]),
    ([1], [0.1], [[0, 1]]), ([1e308], [1e308], [[1]]),
])
def test_conditional_performance_rejects_invalid_or_unsupported_evidence(scores, outcomes, probabilities):
    with pytest.raises(ContractError):
        conditional_performance(scores, outcomes, probabilities)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None])
def test_diagnostics_cannot_hide_missing_numerical_evidence(bad):
    calls = (
        lambda: rank_ic([1, bad], [1, 2]),
        lambda: similarity([1, bad], [1, 2]),
        lambda: turnover([1, bad], [1, 2]),
        lambda: stability_by_fold([0.1, bad]),
        lambda: trial_adjusted_pvalue(bad, 1),
        lambda: capacity_proxy([100, bad], [0.1, 0.1]),
    )
    for call in calls:
        with pytest.raises(ContractError):
            call()
