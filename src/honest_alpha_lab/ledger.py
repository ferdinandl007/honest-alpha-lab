"""Append-only, hash-chained ledgers for experiments and alpha lifecycle events."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Generic, TypeVar
from uuid import uuid4

from .contracts import (
    AgentKind,
    AlphaCandidate,
    AlphaStatus,
    ContractError,
    RegistryEvent,
    TrialRecord,
    TrialStatus,
    canonical_hash,
    utc_now,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LedgerEntry(Generic[T]):
    sequence: int
    entry_id: str
    payload: T
    previous_hash: str
    entry_hash: str
    created_at: datetime


class ImmutableLedger(Generic[T]):
    """In-memory reference ledger; production storage should preserve the same contract."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry[T]] = []
        self._lock = RLock()

    def append(self, payload: T) -> LedgerEntry[T]:
        with self._lock:
            payload = deepcopy(payload)
            previous_hash = self._entries[-1].entry_hash if self._entries else "GENESIS"
            sequence = len(self._entries)
            entry_id = str(uuid4())
            entry_hash = canonical_hash(
                {
                    "sequence": sequence,
                    "entry_id": entry_id,
                    "payload": payload,
                    "previous_hash": previous_hash,
                }
            )
            entry = LedgerEntry(
                sequence, entry_id, payload, previous_hash, entry_hash, utc_now()
            )
            self._entries.append(entry)
            return deepcopy(entry)

    def entries(self) -> tuple[LedgerEntry[T], ...]:
        with self._lock:
            return deepcopy(tuple(self._entries))

    def verify(self) -> bool:
        with self._lock:
            previous_hash = "GENESIS"
            for expected_sequence, entry in enumerate(self._entries):
                if (
                    entry.sequence != expected_sequence
                    or entry.previous_hash != previous_hash
                ):
                    return False
                expected_hash = canonical_hash(
                    {
                        "sequence": entry.sequence,
                        "entry_id": entry.entry_id,
                        "payload": entry.payload,
                        "previous_hash": entry.previous_hash,
                    }
                )
                if expected_hash != entry.entry_hash:
                    return False
                previous_hash = entry.entry_hash
            return True


class AlphaRegistry:
    """Append-only alpha registry with an explicit, numerical-only promotion actor."""

    _ALLOWED: dict[AlphaStatus, frozenset[AlphaStatus]] = {
        AlphaStatus.PROPOSED: frozenset({AlphaStatus.SCREENED, AlphaStatus.REJECTED}),
        AlphaStatus.SCREENED: frozenset({AlphaStatus.VALIDATED, AlphaStatus.REJECTED}),
        AlphaStatus.VALIDATED: frozenset({AlphaStatus.ACCEPTED, AlphaStatus.REJECTED}),
        AlphaStatus.ACCEPTED: frozenset({AlphaStatus.RETIRED}),
        AlphaStatus.REJECTED: frozenset(),
        AlphaStatus.RETIRED: frozenset(),
    }

    def __init__(self) -> None:
        self._candidates: dict[str, AlphaCandidate] = {}
        self._events: ImmutableLedger[RegistryEvent] = ImmutableLedger()
        self._lock = RLock()

    def register(self, candidate: AlphaCandidate) -> AlphaCandidate:
        with self._lock:
            candidate = deepcopy(candidate)
            if candidate.status != AlphaStatus.PROPOSED:
                raise ContractError("new candidates must enter the registry as proposed")
            if candidate.candidate_id in self._candidates:
                raise ContractError("candidate ids are immutable and cannot be reused")
            self._candidates[candidate.candidate_id] = candidate
            self._record_event(
                candidate.candidate_id,
                None,
                AlphaStatus.PROPOSED,
                "research-agent",
                "candidate registered",
            )
            return deepcopy(candidate)

    def register_batch(self, candidates: tuple[AlphaCandidate, ...]) -> tuple[AlphaCandidate, ...]:
        """Validate the entire batch under the registry lock before publication."""
        with self._lock:
            candidates = deepcopy(tuple(candidates))
            ids = [candidate.candidate_id for candidate in candidates]
            if len(set(ids)) != len(ids) or any(key in self._candidates for key in ids):
                raise ContractError("candidate ids are immutable and cannot be reused")
            if any(candidate.status != AlphaStatus.PROPOSED for candidate in candidates):
                raise ContractError("new candidates must enter the registry as proposed")
            return tuple(self.register(candidate) for candidate in candidates)

    def transition(
        self, candidate_id: str, to_status: AlphaStatus, actor: str, reason: str
    ) -> AlphaCandidate:
        with self._lock:
            if actor != "numerical-validator" and to_status in {
                AlphaStatus.VALIDATED,
                AlphaStatus.ACCEPTED,
            }:
                raise PermissionError(
                    "only the numerical validator may validate or accept alpha"
                )
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                raise KeyError(candidate_id)
            if to_status not in self._ALLOWED[candidate.status]:
                raise ContractError(
                    f"invalid lifecycle transition {candidate.status.value} -> {to_status.value}"
                )
            updated = AlphaCandidate(
                candidate_id=candidate.candidate_id,
                agent_kind=candidate.agent_kind,
                name=candidate.name,
                specification=candidate.specification,
                input_snapshot_hash=candidate.input_snapshot_hash,
                lineage_ids=candidate.lineage_ids,
                created_at=candidate.created_at,
                status=to_status,
            )
            self._candidates[candidate_id] = updated
            self._record_event(candidate_id, candidate.status, to_status, actor, reason)
            return deepcopy(updated)

    def get(self, candidate_id: str) -> AlphaCandidate:
        with self._lock:
            return deepcopy(self._candidates[candidate_id])

    def all(self) -> tuple[AlphaCandidate, ...]:
        with self._lock:
            return deepcopy(tuple(self._candidates.values()))

    def events(self) -> tuple[LedgerEntry[RegistryEvent], ...]:
        return self._events.entries()

    def verify(self) -> bool:
        return self._events.verify()

    def _record_event(
        self,
        candidate_id: str,
        from_status: AlphaStatus | None,
        to_status: AlphaStatus,
        actor: str,
        reason: str,
    ) -> None:
        event_without_hash = {
            "candidate_id": candidate_id,
            "from_status": from_status,
            "to_status": to_status,
            "actor": actor,
            "reason": reason,
        }
        event = RegistryEvent(
            candidate_id,
            from_status,
            to_status,
            actor,
            reason,
            canonical_hash(event_without_hash),
        )
        self._events.append(event)


class TrialLedger:
    """Convenience facade enforcing one immutable record per trial state transition."""

    def __init__(self) -> None:
        self._ledger: ImmutableLedger[TrialRecord] = ImmutableLedger()
        self._active: dict[str, TrialRecord] = {}
        self._lock = RLock()

    def start(
        self,
        candidate_id: str,
        agent_kind: AgentKind,
        snapshot_hash: str,
        policy_hash: str,
    ) -> TrialRecord:
        trial = TrialRecord(
            str(uuid4()),
            candidate_id,
            agent_kind,
            snapshot_hash,
            policy_hash,
            TrialStatus.STARTED,
        )
        with self._lock:
            self._ledger.append(trial)
            self._active[trial.trial_id] = deepcopy(trial)
        return trial

    def complete(self, trial: TrialRecord, metrics: dict[str, float]) -> TrialRecord:
        if trial.status != TrialStatus.STARTED:
            raise ContractError("only started trials may complete")
        completed = TrialRecord(
            trial.trial_id,
            trial.candidate_id,
            trial.agent_kind,
            trial.snapshot_hash,
            trial.policy_hash,
            TrialStatus.COMPLETED,
            dict(metrics),
            trial.started_at,
            utc_now(),
        )
        return self._finish(trial, completed)

    def fail(self, trial: TrialRecord, error: str) -> TrialRecord:
        if trial.status != TrialStatus.STARTED:
            raise ContractError("only started trials may fail")
        failed = TrialRecord(
            trial.trial_id,
            trial.candidate_id,
            trial.agent_kind,
            trial.snapshot_hash,
            trial.policy_hash,
            TrialStatus.FAILED,
            started_at=trial.started_at,
            completed_at=utc_now(),
            error=error,
        )
        return self._finish(trial, failed)

    def _finish(self, started: TrialRecord, terminal: TrialRecord) -> TrialRecord:
        with self._lock:
            if self._active.get(started.trial_id) != started:
                raise ContractError("trial is unknown, changed, or already finished")
            self._ledger.append(terminal)
            del self._active[started.trial_id]
            return terminal

    def entries(self) -> tuple[LedgerEntry[TrialRecord], ...]:
        return self._ledger.entries()

    def verify(self) -> bool:
        return self._ledger.verify()
