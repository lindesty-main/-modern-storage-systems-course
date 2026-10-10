#!/usr/bin/env python3
"""Deterministic, in-memory teaching model of a small Raft cluster.

This deliberately omits production concerns such as timers, durable storage,
real transport, and pipelined messages. All state changes happen through
explicit events and a FIFO message queue; it is not a Raft implementation.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class LogEntry:
    term: int
    kind: str
    value: str | tuple[tuple[str, ...], tuple[str, ...]]


@dataclass(frozen=True)
class RequestVote:
    term: int
    candidate_id: str
    last_log_index: int
    last_log_term: int


@dataclass(frozen=True)
class VoteResponse:
    term: int
    voter_id: str
    granted: bool


@dataclass(frozen=True)
class AppendEntries:
    term: int
    leader_id: str
    prev_log_index: int
    prev_log_term: int
    entries: tuple[LogEntry, ...]
    leader_commit: int


@dataclass(frozen=True)
class AppendResponse:
    term: int
    success: bool
    match_index: int
    conflict_index: int


@dataclass(frozen=True)
class InstallSnapshot:
    term: int
    leader_id: str
    last_included_index: int
    last_included_term: int
    applied_commands: tuple[str, ...]
    voters: tuple[str, ...]
    joint_voters: tuple[tuple[str, ...], tuple[str, ...]] | None


@dataclass(frozen=True)
class SnapshotResponse:
    term: int
    success: bool
    match_index: int


@dataclass(frozen=True)
class Envelope:
    sender: str
    recipient: str
    message: object


class RaftNode:
    """One simulated Raft participant; the containing cluster owns transport."""

    def __init__(self, node_id: str, cluster: RaftCluster):
        self.id = node_id
        self.cluster = cluster
        self.alive = True

        # Persistent Raft state (in this model, retained across crash/restart).
        self.current_term = 0
        self.voted_for: str | None = None
        self.log: list[LogEntry] = []
        self.snapshot_index = 0
        self.snapshot_term = 0
        self.snapshot_commands: tuple[str, ...] = ()
        self.snapshot_voters: tuple[str, ...] = ()
        self.snapshot_joint_voters: tuple[tuple[str, ...], tuple[str, ...]] | None = None

        # Volatile state.
        self.role = "follower"
        self.leader_id: str | None = None
        self.commit_index = 0
        self.last_applied = 0
        self.applied_commands: list[str] = []
        self.votes_received: set[str] = set()
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}

    @property
    def last_index(self) -> int:
        return self.snapshot_index + len(self.log)

    @property
    def last_log_term(self) -> int:
        return self.term_at(self.last_index) or 0

    def term_at(self, index: int) -> int | None:
        if index == 0:
            return 0
        if index == self.snapshot_index and index > 0:
            return self.snapshot_term
        if index < self.snapshot_index or index > self.last_index:
            return None
        return self.log[index - self.snapshot_index - 1].term

    def entry_at(self, index: int) -> LogEntry | None:
        if index <= self.snapshot_index or index > self.last_index:
            return None
        return self.log[index - self.snapshot_index - 1]

    def _entries_from(self, index: int) -> tuple[LogEntry, ...]:
        start = max(index, self.snapshot_index + 1)
        if start > self.last_index:
            return ()
        return tuple(self.log[start - self.snapshot_index - 1 :])

    def _send(self, recipient: str, message: object) -> None:
        if self.alive:
            self.cluster.send(self.id, recipient, message)

    def _step_down(self, term: int, leader_id: str | None = None) -> None:
        if term > self.current_term:
            self.current_term = term
            self.voted_for = None
        self.role = "follower"
        self.leader_id = leader_id
        self.votes_received.clear()

    def start_election(self) -> None:
        if not self.alive:
            raise RuntimeError(f"node {self.id} is down")
        if self.id not in self.cluster.eligible_voters:
            raise RuntimeError(f"node {self.id} is not a voter")

        self.current_term += 1
        self.role = "candidate"
        self.leader_id = None
        self.voted_for = self.id
        self.votes_received = {self.id}
        self.cluster.record(
            f"{self.id}: election starts for term {self.current_term}; self-vote"
        )
        if self.cluster.has_quorum(self.votes_received):
            self._become_leader()
            return

        request = RequestVote(
            term=self.current_term,
            candidate_id=self.id,
            last_log_index=self.last_index,
            last_log_term=self.last_log_term,
        )
        for peer_id in sorted(self.cluster.nodes):
            if peer_id != self.id:
                self._send(peer_id, request)

    def _become_leader(self) -> None:
        self.role = "leader"
        self.leader_id = self.id
        self.votes_received.clear()
        self.next_index = {
            peer_id: self.last_index + 1
            for peer_id in self.cluster.nodes
            if peer_id != self.id
        }
        self.match_index = {
            peer_id: (self.last_index if peer_id == self.id else 0)
            for peer_id in self.cluster.nodes
        }
        self.cluster.record(f"{self.id}: becomes leader for term {self.current_term}")
        self.cluster.check_leader_completeness(self)
        self._broadcast_append()

    def _broadcast_append(self) -> None:
        if self.role != "leader" or not self.alive:
            return
        for peer_id in sorted(self.cluster.nodes):
            if peer_id != self.id:
                self._send_append(peer_id)

    def _send_append(self, peer_id: str) -> None:
        if self.role != "leader" or not self.alive:
            return
        next_index = self.next_index.setdefault(peer_id, 1)
        self.match_index.setdefault(peer_id, 0)
        if next_index <= self.snapshot_index:
            message = InstallSnapshot(
                term=self.current_term,
                leader_id=self.id,
                last_included_index=self.snapshot_index,
                last_included_term=self.snapshot_term,
                applied_commands=self.snapshot_commands,
                voters=tuple(sorted(self.cluster.voters)),
                joint_voters=(
                    tuple(sorted(self.cluster.joint_voters[0])),
                    tuple(sorted(self.cluster.joint_voters[1])),
                )
                if self.cluster.joint_voters is not None
                else None,
            )
            self.cluster.record(
                f"{self.id}: sends snapshot through index {self.snapshot_index} to {peer_id}"
            )
            self._send(peer_id, message)
            return

        prev_index = next_index - 1
        prev_term = self.term_at(prev_index)
        if prev_term is None:
            # The follower is behind the compacted prefix; try the snapshot.
            self.next_index[peer_id] = min(next_index, self.snapshot_index)
            self._send_append(peer_id)
            return
        message = AppendEntries(
            term=self.current_term,
            leader_id=self.id,
            prev_log_index=prev_index,
            prev_log_term=prev_term,
            entries=self._entries_from(next_index),
            leader_commit=self.commit_index,
        )
        self._send(peer_id, message)

    def _handle(self, message: object, sender_id: str) -> None:
        if isinstance(message, RequestVote):
            self._handle_request_vote(message)
        elif isinstance(message, VoteResponse):
            self._handle_vote_response(message)
        elif isinstance(message, AppendEntries):
            self._handle_append_entries(message)
        elif isinstance(message, AppendResponse):
            self._handle_append_response(message, sender_id)
        elif isinstance(message, InstallSnapshot):
            self._handle_install_snapshot(message)
        elif isinstance(message, SnapshotResponse):
            self._handle_snapshot_response(message, sender_id)
        else:
            raise TypeError(f"unsupported message: {message!r}")

    def _handle_request_vote(self, message: RequestVote) -> None:
        if message.term > self.current_term:
            self._step_down(message.term)

        can_vote = (
            message.term == self.current_term
            and self.id in self.cluster.eligible_voters
            and message.candidate_id in self.cluster.eligible_voters
            and (self.voted_for is None or self.voted_for == message.candidate_id)
        )
        local_version = (self.last_log_term, self.last_index)
        candidate_version = (message.last_log_term, message.last_log_index)
        granted = can_vote and candidate_version >= local_version
        if granted:
            self.voted_for = message.candidate_id
            self.role = "follower"
            self.leader_id = None
            self.cluster.record(
                f"{self.id}: grants term {message.term} vote to {message.candidate_id}"
            )
        else:
            self.cluster.record(
                f"{self.id}: rejects term {message.term} vote for {message.candidate_id}"
            )
        self._send(
            message.candidate_id,
            VoteResponse(self.current_term, self.id, granted),
        )

    def _handle_vote_response(self, message: VoteResponse) -> None:
        if message.term > self.current_term:
            self._step_down(message.term)
            return
        if (
            self.role != "candidate"
            or message.term != self.current_term
            or not message.granted
            or message.voter_id not in self.cluster.eligible_voters
        ):
            return
        self.votes_received.add(message.voter_id)
        if self.cluster.has_quorum(self.votes_received):
            self._become_leader()

    def _handle_append_entries(self, message: AppendEntries) -> None:
        if message.term < self.current_term:
            self._send(
                message.leader_id,
                AppendResponse(self.current_term, False, 0, self.last_index + 1),
            )
            return

        self._step_down(message.term, message.leader_id)
        self.cluster.record(
            f"{self.id}: accepts AppendEntries from {message.leader_id} "
            f"in term {message.term}"
        )

        prev = message.prev_log_index
        if prev < self.snapshot_index:
            self._send(
                message.leader_id,
                AppendResponse(
                    self.current_term, False, 0, self.snapshot_index + 1
                ),
            )
            return
        if prev > self.last_index:
            self._send(
                message.leader_id,
                AppendResponse(self.current_term, False, 0, self.last_index + 1),
            )
            return
        if self.term_at(prev) != message.prev_log_term:
            conflict_term = self.term_at(prev)
            conflict_index = prev
            while conflict_index > self.snapshot_index + 1:
                if self.term_at(conflict_index - 1) != conflict_term:
                    break
                conflict_index -= 1
            self._send(
                message.leader_id,
                AppendResponse(self.current_term, False, 0, conflict_index),
            )
            return

        append_from = len(message.entries)
        for offset, incoming in enumerate(message.entries):
            index = prev + offset + 1
            if index <= self.snapshot_index:
                continue
            existing = self.entry_at(index)
            if existing is None:
                append_from = offset
                break
            if existing.term != incoming.term:
                if index <= self.commit_index:
                    raise AssertionError("AppendEntries attempted to truncate committed log")
                del self.log[index - self.snapshot_index - 1 :]
                append_from = offset
                break
            if existing != incoming:
                raise AssertionError("same-term/index entries must be identical")
        if append_from < len(message.entries):
            self.log.extend(message.entries[append_from:])

        if message.leader_commit > self.commit_index:
            self._advance_commit(min(message.leader_commit, self.last_index))
        matched = prev + len(message.entries)
        self._send(
            message.leader_id,
            AppendResponse(self.current_term, True, matched, 0),
        )

    def _handle_append_response(self, message: AppendResponse, peer_id: str) -> None:
        if message.term > self.current_term:
            self._step_down(message.term)
            return
        if self.role != "leader" or message.term != self.current_term:
            return

        if peer_id == self.id:
            return
        if message.success:
            self.match_index[peer_id] = max(
                self.match_index.get(peer_id, 0), message.match_index
            )
            self.next_index[peer_id] = self.match_index[peer_id] + 1
            self._advance_leader_commit()
        else:
            current = self.next_index.get(peer_id, self.last_index + 1)
            reduced = max(1, min(current - 1, message.conflict_index))
            if reduced < current:
                self.next_index[peer_id] = reduced
                self._send_append(peer_id)

    def _handle_install_snapshot(self, message: InstallSnapshot) -> None:
        if message.term < self.current_term:
            self._send(
                message.leader_id,
                SnapshotResponse(self.current_term, False, self.last_index),
            )
            return
        self._step_down(message.term, message.leader_id)

        if message.last_included_index <= self.snapshot_index:
            self._send(
                message.leader_id,
                SnapshotResponse(self.current_term, True, self.last_index),
            )
            return
        if message.last_included_index <= self.commit_index:
            # Never replace already-committed local history with an older or
            # non-matching snapshot.
            if self.term_at(message.last_included_index) != message.last_included_term:
                self._send(
                    message.leader_id,
                    SnapshotResponse(self.current_term, False, self.last_index),
                )
                return

        keep_suffix = (
            self.term_at(message.last_included_index) == message.last_included_term
        )
        if keep_suffix:
            self.log = [
                entry
                for index, entry in enumerate(
                    self.log, start=self.snapshot_index + 1
                )
                if index > message.last_included_index
            ]
        else:
            self.log = []
        self.snapshot_index = message.last_included_index
        self.snapshot_term = message.last_included_term
        self.snapshot_commands = message.applied_commands
        self.snapshot_voters = message.voters
        self.snapshot_joint_voters = message.joint_voters
        self.applied_commands = list(message.applied_commands)
        self.commit_index = max(self.commit_index, self.snapshot_index)
        self.last_applied = self.snapshot_index
        while self.last_applied < self.commit_index:
            index = self.last_applied + 1
            entry = self.entry_at(index)
            if entry is None:
                raise AssertionError(f"committed entry {index} is missing after snapshot")
            if entry.kind == "command":
                self.applied_commands.append(str(entry.value))
            self.last_applied = index
        self.cluster.record(
            f"{self.id}: installs snapshot through index {self.snapshot_index}"
        )
        self._send(
            message.leader_id,
            SnapshotResponse(self.current_term, True, self.snapshot_index),
        )

    def _handle_snapshot_response(self, message: SnapshotResponse, peer_id: str) -> None:
        if message.term > self.current_term:
            self._step_down(message.term)
            return
        if self.role != "leader" or message.term != self.current_term or not message.success:
            return
        if peer_id == self.id:
            return
        self.match_index[peer_id] = max(
            self.match_index.get(peer_id, 0), message.match_index
        )
        self.next_index[peer_id] = self.match_index[peer_id] + 1
        self._advance_leader_commit()
        self._send_append(peer_id)

    def append_command(self, command: str) -> int:
        if self.role != "leader" or not self.alive:
            raise RuntimeError(f"node {self.id} is not an active leader")
        if self.id not in self.cluster.eligible_voters:
            raise RuntimeError(f"leader {self.id} is not in the active configuration")
        self.log.append(LogEntry(self.current_term, "command", command))
        index = self.last_index
        self.match_index[self.id] = index
        self.cluster.record(f"{self.id}: appends command at index {index}")
        self._broadcast_append()
        self._advance_leader_commit()
        return index

    def append_configuration(
        self,
        kind: str,
        old_voters: tuple[str, ...],
        new_voters: tuple[str, ...],
    ) -> int:
        if self.role != "leader" or not self.alive:
            raise RuntimeError(f"node {self.id} is not an active leader")
        self.log.append(LogEntry(self.current_term, kind, (old_voters, new_voters)))
        index = self.last_index
        self.match_index[self.id] = index
        self.cluster.record(f"{self.id}: appends {kind} configuration at index {index}")
        self._broadcast_append()
        self._advance_leader_commit()
        return index

    def _advance_leader_commit(self) -> None:
        if self.role != "leader" or not self.alive:
            return
        previous_commit = self.commit_index
        for index in range(self.last_index, self.commit_index, -1):
            if self.term_at(index) != self.current_term:
                continue
            acknowledgements = {
                voter
                for voter in self.cluster.eligible_voters
                if self.match_index.get(voter, 0) >= index
            }
            if self.cluster.has_quorum(acknowledgements):
                self._advance_commit(index)
                break
        if self.commit_index > previous_commit:
            self._broadcast_append()

    def _advance_commit(self, new_commit: int) -> None:
        if new_commit <= self.commit_index:
            return
        old_commit = self.commit_index
        self.commit_index = new_commit
        while self.last_applied < self.commit_index:
            index = self.last_applied + 1
            entry = self.entry_at(index)
            if entry is None:
                raise AssertionError(f"committed entry {index} is missing from log")
            if entry.kind == "command":
                self.applied_commands.append(str(entry.value))
            self.last_applied = index
        self.cluster.on_commit(self, old_commit, self.commit_index)

    def make_snapshot(self) -> int:
        if not self.alive:
            raise RuntimeError(f"node {self.id} is down")
        if self.last_applied <= self.snapshot_index:
            raise RuntimeError("no newly applied entries to snapshot")
        index = self.last_applied
        term = self.term_at(index)
        if term is None:
            raise AssertionError("applied index is not present in log")
        self.log = self.log[index - self.snapshot_index :]
        self.snapshot_index = index
        self.snapshot_term = term
        self.snapshot_commands = tuple(self.applied_commands)
        self.snapshot_voters = tuple(sorted(self.cluster.voters))
        self.snapshot_joint_voters = (
            (
                tuple(sorted(self.cluster.joint_voters[0])),
                tuple(sorted(self.cluster.joint_voters[1])),
            )
            if self.cluster.joint_voters is not None
            else None
        )
        self.cluster.record(f"{self.id}: snapshots applied state through index {index}")
        return index

    def crash(self) -> None:
        self.alive = False
        self.role = "follower"
        self.leader_id = None
        self.votes_received.clear()
        self.cluster.record(f"{self.id}: crashes")

    def restart(self) -> None:
        if self.alive:
            return
        self.alive = True
        self.role = "follower"
        self.leader_id = None
        # Terms, vote, log and snapshot persist. Commit/applied progress after
        # the last snapshot is volatile and is learned again from a leader.
        self.commit_index = self.snapshot_index
        self.last_applied = self.snapshot_index
        self.applied_commands = list(self.snapshot_commands)
        self.votes_received.clear()
        self.next_index.clear()
        self.match_index.clear()
        self.cluster.record(f"{self.id}: restarts as follower")


class RaftCluster:
    """Deterministic event harness for the teaching state machine."""

    def __init__(self, node_ids: Iterable[str], voters: Iterable[str] | None = None):
        ids = tuple(node_ids)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("node_ids must be a non-empty sequence of unique IDs")
        voter_set = set(voters if voters is not None else ids)
        if not voter_set or not voter_set.issubset(ids):
            raise ValueError("voters must be a non-empty subset of node_ids")
        self.nodes: dict[str, RaftNode] = {}
        self.voters = voter_set
        self.joint_voters: tuple[set[str], set[str]] | None = None
        self.membership_old_voters: tuple[str, ...] | None = None
        self.membership_new_voters: tuple[str, ...] | None = None
        self.membership_joint_index: int | None = None
        self.membership_final_index: int | None = None
        self.pending_final_voters: set[str] | None = None
        self.queue: deque[Envelope] = deque()
        self.blocked_links: set[frozenset[str]] = set()
        self.trace: list[str] = []
        self.committed_log: dict[int, LogEntry] = {}
        for node_id in ids:
            self.nodes[node_id] = RaftNode(node_id, self)

    @property
    def eligible_voters(self) -> set[str]:
        if self.joint_voters is None:
            return set(self.voters)
        return set(self.joint_voters[0]) | set(self.joint_voters[1])

    def has_quorum(self, acknowledgements: Iterable[str]) -> bool:
        acknowledgements = set(acknowledgements)

        def majority(voters: set[str]) -> bool:
            return len(acknowledgements & voters) >= len(voters) // 2 + 1

        if self.joint_voters is None:
            return majority(self.voters)
        old, new = self.joint_voters
        return majority(old) and majority(new)

    def record(self, event: str) -> None:
        self.trace.append(event)

    def send(self, sender: str, recipient: str, message: object) -> None:
        self.queue.append(Envelope(sender, recipient, message))

    def run(self, max_events: int = 10000) -> int:
        delivered = 0
        while self.queue:
            if delivered >= max_events:
                raise RuntimeError("event limit reached; possible simulator livelock")
            envelope = self.queue.popleft()
            sender = self.nodes[envelope.sender]
            recipient = self.nodes[envelope.recipient]
            link = frozenset((envelope.sender, envelope.recipient))
            if not sender.alive or not recipient.alive:
                self.record(
                    f"drop {envelope.sender}->{envelope.recipient}: node down"
                )
                delivered += 1
                continue
            if link in self.blocked_links:
                self.record(
                    f"drop {envelope.sender}->{envelope.recipient}: partition"
                )
                delivered += 1
                continue
            self.record(
                f"deliver {envelope.sender}->{envelope.recipient}: "
                f"{type(envelope.message).__name__}"
            )
            recipient._handle(envelope.message, envelope.sender)
            delivered += 1
        return delivered

    def partition(self, *groups: Iterable[str]) -> None:
        normalized = [tuple(sorted(set(group))) for group in groups]
        if any(not set(group).issubset(self.nodes) for group in normalized):
            raise ValueError("partition contains unknown node")
        for left_index, left in enumerate(normalized):
            for right in normalized[left_index + 1 :]:
                for left_id in left:
                    for right_id in right:
                        self.blocked_links.add(frozenset((left_id, right_id)))
        self.record(f"network partition: {normalized}")

    def heal(self) -> None:
        self.blocked_links.clear()
        self.record("network heals")

    def start_election(self, node_id: str) -> None:
        self.nodes[node_id].start_election()

    def propose(self, command: str, leader_id: str | None = None) -> int:
        if leader_id is None:
            leaders = self.leaders()
            if len(leaders) != 1:
                raise RuntimeError("specify leader_id when there is not exactly one leader")
            leader_id = leaders[0].id
        return self.nodes[leader_id].append_command(command)

    def replicate_from(self, leader_id: str) -> None:
        leader = self.nodes[leader_id]
        if leader.role != "leader" or not leader.alive:
            raise RuntimeError(f"node {leader_id} is not an active leader")
        leader._broadcast_append()

    def crash(self, node_id: str) -> None:
        self.nodes[node_id].crash()

    def restart(self, node_id: str) -> None:
        self.nodes[node_id].restart()

    def snapshot(self, node_id: str) -> int:
        return self.nodes[node_id].make_snapshot()

    def add_node(self, node_id: str) -> RaftNode:
        if node_id in self.nodes:
            raise ValueError(f"node {node_id} already exists")
        node = RaftNode(node_id, self)
        self.nodes[node_id] = node
        for leader in self.leaders():
            leader.next_index[node_id] = 1
            leader.match_index[node_id] = 0
            leader._send_append(node_id)
        self.record(f"{node_id}: added as non-voter")
        return node

    def begin_membership_change(
        self, new_voters: Iterable[str], leader_id: str | None = None
    ) -> int:
        """Append C_old,new and require both configurations' majorities to commit it."""
        if self.joint_voters is not None or self.pending_final_voters is not None:
            raise RuntimeError("finish the active membership change first")
        if leader_id is None:
            leaders = self.leaders()
            if len(leaders) != 1:
                raise RuntimeError("specify leader_id when there is not exactly one leader")
            leader_id = leaders[0].id
        leader = self.nodes[leader_id]
        if leader.role != "leader" or not leader.alive:
            raise RuntimeError(f"node {leader_id} is not an active leader")
        new_set = set(new_voters)
        if not new_set or not new_set.issubset(self.nodes):
            raise ValueError("new_voters must be a non-empty subset of known nodes")
        if leader_id not in new_set:
            raise ValueError("teaching model requires the leader to remain in new_voters")

        old_tuple = tuple(sorted(self.voters))
        new_tuple = tuple(sorted(new_set))
        self.membership_old_voters = old_tuple
        self.membership_new_voters = new_tuple
        self.joint_voters = (set(old_tuple), set(new_tuple))
        self.membership_joint_index = leader.last_index + 1
        joint_index = leader.append_configuration("joint", old_tuple, new_tuple)
        if joint_index != self.membership_joint_index:
            raise AssertionError("unexpected joint configuration index")
        self.run()
        if leader.commit_index < joint_index:
            raise RuntimeError("joint configuration did not commit under both majorities")
        return joint_index

    def change_membership(
        self, new_voters: Iterable[str], leader_id: str | None = None
    ) -> None:
        self.begin_membership_change(new_voters, leader_id)
        self.finalize_membership(leader_id)

    def finalize_membership(self, leader_id: str | None = None) -> None:
        if self.joint_voters is None and self.pending_final_voters is None:
            raise RuntimeError("there is no active membership change")
        if leader_id is None:
            leaders = self.leaders()
            if len(leaders) != 1:
                raise RuntimeError("specify leader_id when there is not exactly one leader")
            leader_id = leaders[0].id
        leader = self.nodes[leader_id]
        if leader.role != "leader" or not leader.alive:
            raise RuntimeError(f"node {leader_id} is not an active leader")

        if self.joint_voters is not None:
            joint_index = self.membership_joint_index
            if joint_index is None:
                raise AssertionError("joint configuration index is missing")
            if leader.commit_index < joint_index:
                leader._broadcast_append()
                self.run()
            if leader.commit_index < joint_index:
                raise RuntimeError("joint configuration still lacks both majorities")

            old_tuple = self.membership_old_voters
            new_tuple = self.membership_new_voters
            if old_tuple is None or new_tuple is None:
                raise AssertionError("membership transition metadata is missing")
            # Raft's paper model begins using C_new when the final configuration
            # entry is added. This teaching harness switches centrally here.
            self.voters = set(new_tuple)
            self.joint_voters = None
            self.pending_final_voters = set(new_tuple)
            self.membership_final_index = None
            self.record(f"configuration begins final C_new phase: {new_tuple}")

        old_tuple = self.membership_old_voters
        new_tuple = self.membership_new_voters
        if old_tuple is None or new_tuple is None:
            raise AssertionError("membership transition metadata is missing")
        if self.membership_final_index is None:
            planned_final_index = leader.last_index + 1
            self.membership_final_index = planned_final_index
            final_index = leader.append_configuration("final", old_tuple, new_tuple)
            if final_index != planned_final_index:
                raise AssertionError("unexpected final configuration index")
        else:
            final_index = self.membership_final_index
            leader._broadcast_append()
        self.run()
        if leader.commit_index < final_index or self.pending_final_voters is not None:
            raise RuntimeError("final configuration did not commit under new quorum")

    def on_commit(self, node: RaftNode, old_commit: int, new_commit: int) -> None:
        if node.role == "leader":
            for index in range(old_commit + 1, new_commit + 1):
                entry = node.entry_at(index)
                if entry is not None:
                    self.committed_log[index] = entry

        for index in range(old_commit + 1, new_commit + 1):
            entry = node.entry_at(index)
            if entry is None:
                continue
            if entry.kind == "joint":
                old_tuple, new_tuple = entry.value  # type: ignore[misc]
                joint = (set(old_tuple), set(new_tuple))
                is_active_joint = self.joint_voters == joint
                is_later_new_config = (
                    self.voters == set(new_tuple)
                    and (
                        self.pending_final_voters is None
                        or self.pending_final_voters == set(new_tuple)
                    )
                )
                if not (is_active_joint or is_later_new_config):
                    raise AssertionError("committed joint entry does not match active config")
                if self.membership_joint_index is None and is_active_joint:
                    self.membership_joint_index = index
                self.record(f"joint configuration committed at index {index}: {old_tuple} -> {new_tuple}")
            elif entry.kind == "final":
                old_tuple, new_tuple = entry.value  # type: ignore[misc]
                new_set = set(new_tuple)
                if self.voters != new_set:
                    raise AssertionError("final entry does not match active new config")
                if self.pending_final_voters is not None:
                    self.pending_final_voters = None
                    self.membership_old_voters = None
                    self.membership_new_voters = None
                    self.membership_joint_index = None
                    self.membership_final_index = None
                    self.record(f"new configuration committed: {new_tuple}")

    def check_leader_completeness(self, leader: RaftNode) -> None:
        for index, committed_entry in self.committed_log.items():
            if index <= leader.snapshot_index:
                continue
            if leader.entry_at(index) != committed_entry:
                raise AssertionError(
                    f"new leader {leader.id} lacks committed entry {index}"
                )

    def leaders(self) -> list[RaftNode]:
        return [
            node
            for node in self.nodes.values()
            if node.alive and node.role == "leader"
        ]

    def assert_safety(self) -> None:
        """Check safety invariants visible in this deterministic run."""
        leaders_by_term: dict[int, list[str]] = {}
        for node in self.nodes.values():
            if node.alive and node.role == "leader":
                leaders_by_term.setdefault(node.current_term, []).append(node.id)
            if not (node.snapshot_index <= node.last_applied <= node.commit_index):
                raise AssertionError(f"invalid apply/commit indexes on {node.id}")
            if node.commit_index > node.last_index:
                raise AssertionError(f"commit index exceeds log on {node.id}")
            if node.last_index != node.snapshot_index + len(node.log):
                raise AssertionError(f"non-contiguous log on {node.id}")

            for index, committed_entry in self.committed_log.items():
                if index <= node.snapshot_index or index > node.commit_index:
                    continue
                if node.entry_at(index) != committed_entry:
                    raise AssertionError(
                        f"{node.id} has a conflicting committed entry at {index}"
                    )
        if any(len(node_ids) > 1 for node_ids in leaders_by_term.values()):
            raise AssertionError(f"multiple leaders in one term: {leaders_by_term}")

        applied = [node for node in self.nodes.values() if node.alive]
        for left_index, left in enumerate(applied):
            for right in applied[left_index + 1 :]:
                common_applied = min(left.last_applied, right.last_applied)
                if left.applied_commands[:common_applied] != right.applied_commands[:common_applied]:
                    raise AssertionError(
                        f"state machine divergence between {left.id} and {right.id}"
                    )


def run_demo() -> None:
    cluster = RaftCluster(("n1", "n2", "n3"))
    cluster.start_election("n1")
    cluster.run()
    index = cluster.propose("put block-7 checksum=abc123", leader_id="n1")
    cluster.run()
    cluster.crash("n3")
    print(f"leader=n1 term={cluster.nodes['n1'].current_term}")
    print(f"committed_index={cluster.nodes['n1'].commit_index} proposal_index={index}")
    print(f"applied={cluster.nodes['n1'].applied_commands}")
    cluster.assert_safety()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run a deterministic three-node example")
    args = parser.parse_args()
    if not args.demo:
        parser.error("select --demo to run the deterministic example")
    run_demo()


if __name__ == "__main__":
    main()
