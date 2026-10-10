"""Deterministic teaching model for MVCC and Percolator-inspired transactions.

This is an in-memory model, not a TiDB/TiKV implementation.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


class TransactionError(Exception):
    """Base class for modeled transaction failures."""


class WriteConflict(TransactionError):
    """A newer committed version conflicts with this transaction's write."""


class KeyLocked(TransactionError):
    """A different unresolved transaction owns a key lock."""


class ParticipantUnavailable(TransactionError):
    """The participant that stores a requested key is offline."""


class TransactionBlocked(TransactionError):
    """The outcome cannot be learned safely while its primary is unavailable/unknown."""


@dataclass(frozen=True)
class Version:
    commit_ts: int
    value: str


@dataclass(frozen=True)
class Lock:
    txid: str
    start_ts: int
    primary_key: str
    value: str


@dataclass(frozen=True)
class Decision:
    committed: bool
    commit_ts: Optional[int] = None


@dataclass
class Participant:
    name: str
    online: bool = True
    versions: Dict[str, list[Version]] = field(default_factory=dict)
    locks: Dict[str, Lock] = field(default_factory=dict)
    # A transaction decision is recorded at its primary participant.
    decisions: Dict[str, Decision] = field(default_factory=dict)


class MVCCCluster:
    """Fixed-key-to-participant cluster with logical timestamps and explicit failures."""

    def __init__(self, key_to_participant: Dict[str, str]):
        if not key_to_participant:
            raise ValueError("at least one key is required")
        self.key_to_participant = dict(key_to_participant)
        names = sorted(set(key_to_participant.values()))
        self.participants = {name: Participant(name) for name in names}
        self._clock = 0
        self._transactions: Dict[str, Transaction] = {}

    @property
    def clock(self) -> int:
        return self._clock

    def _next_ts(self) -> int:
        self._clock += 1
        return self._clock

    def _participant_for(self, key: str) -> Participant:
        try:
            return self.participants[self.key_to_participant[key]]
        except KeyError as exc:
            raise ValueError(f"unknown key: {key}") from exc

    @staticmethod
    def _require_online(participant: Participant) -> None:
        if not participant.online:
            raise ParticipantUnavailable(participant.name)

    def seed(self, key: str, value: str) -> int:
        """Install an initial committed value for a deterministic exercise."""
        participant = self._participant_for(key)
        self._require_online(participant)
        if key in participant.locks:
            raise KeyLocked(key)
        commit_ts = self._next_ts()
        participant.versions.setdefault(key, []).append(Version(commit_ts, value))
        return commit_ts

    def begin(self, txid: str) -> "Transaction":
        if txid in self._transactions:
            raise ValueError(f"duplicate transaction id: {txid}")
        tx = Transaction(self, txid, self._next_ts())
        self._transactions[txid] = tx
        return tx

    def read(self, key: str, read_ts: int) -> Optional[str]:
        """Read the newest committed version visible at read_ts, resolving known locks."""
        participant = self._participant_for(key)
        self._require_online(participant)
        lock = participant.locks.get(key)
        if lock is not None:
            self._resolve_lock(key, participant, lock)

        visible = [v for v in participant.versions.get(key, ()) if v.commit_ts <= read_ts]
        if not visible:
            return None
        return max(visible, key=lambda version: version.commit_ts).value

    def crash(self, participant_name: str) -> None:
        self.participants[participant_name].online = False

    def restart(self, participant_name: str) -> None:
        self.participants[participant_name].online = True

    def _decision_for(self, lock: Lock) -> Optional[Decision]:
        primary = self._participant_for(lock.primary_key)
        if not primary.online:
            return None
        return primary.decisions.get(lock.txid)

    def _resolve_lock(self, key: str, participant: Participant, lock: Lock) -> None:
        decision = self._decision_for(lock)
        if decision is None:
            raise TransactionBlocked(
                f"{key} is locked by {lock.txid}; primary decision is not available"
            )
        if decision.committed:
            participant.versions.setdefault(key, []).append(
                Version(decision.commit_ts, lock.value)
            )
        participant.locks.pop(key, None)

    def _abort_prepared(self, tx: "Transaction") -> None:
        """Record an abort at the primary and release every reachable lock."""
        primary_key = tx.primary_key
        if primary_key is None:
            tx.state = "aborted"
            tx.prepared_keys.clear()
            return
        primary = self._participant_for(primary_key)
        existing = primary.decisions.get(tx.txid)
        if existing is not None:
            if existing.committed:
                raise TransactionBlocked("a committed primary decision cannot be rolled back")
            tx.state = "aborted"
            tx.prepared_keys.clear()
            return

        has_locks = any(
            lock.txid == tx.txid
            for participant in self.participants.values()
            for lock in participant.locks.values()
        )
        if has_locks and not primary.online:
            raise TransactionBlocked("cannot record abort while the primary is unavailable")
        if primary.online:
            primary.decisions[tx.txid] = Decision(committed=False)
        for participant in self.participants.values():
            for key, lock in list(participant.locks.items()):
                if lock.txid == tx.txid and participant.online:
                    participant.locks.pop(key, None)
        tx.state = "aborted"
        tx.prepared_keys.clear()

    def prepare(self, tx: "Transaction") -> None:
        if tx.state != "active":
            raise TransactionError(f"cannot prepare transaction in state {tx.state}")
        if not tx.writes:
            raise TransactionError("cannot prepare a transaction with no writes")

        tx.primary_key = min(tx.writes)
        try:
            # The primary is acquired first; deterministic ordering avoids model deadlocks.
            for key in sorted(tx.writes):
                participant = self._participant_for(key)
                self._require_online(participant)
                existing_lock = participant.locks.get(key)
                if existing_lock is not None and existing_lock.txid != tx.txid:
                    decision = self._decision_for(existing_lock)
                    if decision is not None:
                        self._resolve_lock(key, participant, existing_lock)
                    if key in participant.locks:
                        raise KeyLocked(f"{key} is locked by {existing_lock.txid}")

                versions = participant.versions.get(key, ())
                latest_ts = max((v.commit_ts for v in versions), default=0)
                if latest_ts > tx.start_ts:
                    raise WriteConflict(
                        f"{key} has commit_ts={latest_ts} newer than start_ts={tx.start_ts}"
                    )
                participant.locks[key] = Lock(
                    tx.txid, tx.start_ts, tx.primary_key, tx.writes[key]
                )
                tx.prepared_keys.add(key)
        except TransactionError:
            self._abort_prepared(tx)
            raise
        tx.state = "prepared"

    def commit(self, tx: "Transaction", *, pause_after_primary: bool = False) -> int:
        if tx.state == "active":
            self.prepare(tx)
        if tx.state == "committed":
            if tx.commit_ts is None:
                raise AssertionError("committed transaction lacks commit timestamp")
            if not pause_after_primary:
                self.resolve_secondaries(tx)
            return tx.commit_ts
        if tx.state != "prepared":
            raise TransactionError(f"cannot commit transaction in state {tx.state}")

        primary_key = tx.primary_key
        if primary_key is None:
            raise AssertionError("prepared transaction lacks a primary key")
        primary = self._participant_for(primary_key)
        self._require_online(primary)
        if tx.prepared_keys != set(tx.writes):
            raise TransactionBlocked("not all participants returned a prepared vote")
        primary_lock = primary.locks.get(primary_key)
        if primary_lock is None or primary_lock.txid != tx.txid:
            raise TransactionBlocked(f"missing primary prewrite lock for {primary_key}")

        # A secondary may go offline after returning YES. Its prepared lock remains
        # modeled as durable state; the primary decision can still be recorded.
        commit_ts = self._next_ts()
        primary.decisions[tx.txid] = Decision(committed=True, commit_ts=commit_ts)
        primary.locks.pop(primary_key)
        primary.versions.setdefault(primary_key, []).append(
            Version(commit_ts, primary_lock.value)
        )
        tx.commit_ts = commit_ts
        tx.state = "committed"
        if not pause_after_primary:
            self.resolve_secondaries(tx)
        return commit_ts

    def resolve_secondaries(self, tx: "Transaction") -> None:
        if tx.state != "committed":
            raise TransactionError("only a committed transaction has secondaries to resolve")
        for key in tx.writes:
            if key == tx.primary_key:
                continue
            participant = self._participant_for(key)
            if not participant.online:
                continue  # The durable primary decision permits later lazy resolution.
            lock = participant.locks.get(key)
            if lock is not None and lock.txid == tx.txid:
                self._resolve_lock(key, participant, lock)

    def abort(self, tx: "Transaction") -> None:
        if tx.state == "aborted":
            return
        self._abort_prepared(tx)


class Transaction:
    def __init__(self, cluster: MVCCCluster, txid: str, start_ts: int):
        self.cluster = cluster
        self.txid = txid
        self.start_ts = start_ts
        self.writes: Dict[str, str] = {}
        self.prepared_keys: set[str] = set()
        self.primary_key: Optional[str] = None
        self.commit_ts: Optional[int] = None
        self.state = "active"

    def get(self, key: str) -> Optional[str]:
        if key in self.writes:
            return self.writes[key]
        return self.cluster.read(key, self.start_ts)

    def put(self, key: str, value: str) -> None:
        if self.state != "active":
            raise TransactionError(f"cannot write in transaction state {self.state}")
        self.cluster._participant_for(key)  # Validate key before staging it.
        self.writes[key] = value

    def prepare(self) -> None:
        self.cluster.prepare(self)

    def commit(self, *, pause_after_primary: bool = False) -> int:
        return self.cluster.commit(self, pause_after_primary=pause_after_primary)

    def abort(self) -> None:
        self.cluster.abort(self)
