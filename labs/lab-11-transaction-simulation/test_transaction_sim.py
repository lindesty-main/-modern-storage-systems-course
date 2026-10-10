import unittest

from transaction_sim import (
    KeyLocked,
    MVCCCluster,
    ParticipantUnavailable,
    TransactionBlocked,
    WriteConflict,
)


class MVCCTransactionTests(unittest.TestCase):
    def setUp(self):
        self.cluster = MVCCCluster({"a": "r1", "b": "r2", "c": "r2"})

    def test_snapshot_reads_only_versions_at_start_ts(self):
        self.cluster.seed("a", "old")
        reader = self.cluster.begin("reader")
        writer = self.cluster.begin("writer")
        writer.put("a", "new")
        writer.commit()
        self.assertEqual(reader.get("a"), "old")
        self.assertEqual(self.cluster.read("a", self.cluster.clock), "new")

    def test_transaction_reads_its_own_writes(self):
        self.cluster.seed("a", "before")
        tx = self.cluster.begin("tx")
        tx.put("a", "after")
        self.assertEqual(tx.get("a"), "after")

    def test_first_committer_wins_for_concurrent_writes(self):
        self.cluster.seed("a", "v0")
        first = self.cluster.begin("first")
        second = self.cluster.begin("second")
        first.put("a", "v1")
        second.put("a", "v2")
        first.commit()
        with self.assertRaises(WriteConflict):
            second.commit()
        self.assertEqual(self.cluster.read("a", self.cluster.clock), "v1")
        self.assertNotIn("a", self.cluster.participants["r1"].locks)

    def test_lock_conflict_aborts_partial_prewrite(self):
        blocker = self.cluster.begin("blocker")
        blocker.put("b", "held")
        blocker.prepare()

        contender = self.cluster.begin("contender")
        contender.put("a", "partial")
        contender.put("b", "blocked")
        with self.assertRaises(KeyLocked):
            contender.prepare()
        self.assertNotIn("a", self.cluster.participants["r1"].locks)
        blocker.abort()

    def test_snapshot_isolation_allows_write_skew_on_disjoint_writes(self):
        self.cluster.seed("a", "on")
        self.cluster.seed("b", "on")
        doctor_a = self.cluster.begin("doctor-a")
        doctor_b = self.cluster.begin("doctor-b")

        self.assertEqual(doctor_a.get("a"), "on")
        self.assertEqual(doctor_a.get("b"), "on")
        self.assertEqual(doctor_b.get("a"), "on")
        self.assertEqual(doctor_b.get("b"), "on")
        doctor_a.put("a", "off")
        doctor_b.put("b", "off")
        doctor_a.commit()
        doctor_b.commit()

        self.assertEqual(self.cluster.read("a", self.cluster.clock), "off")
        self.assertEqual(self.cluster.read("b", self.cluster.clock), "off")

    def test_explicit_abort_keeps_old_committed_value(self):
        self.cluster.seed("a", "stable")
        tx = self.cluster.begin("tx")
        tx.put("a", "discarded")
        tx.prepare()
        tx.abort()
        self.assertEqual(self.cluster.read("a", self.cluster.clock), "stable")


class PercolatorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cluster = MVCCCluster({"a": "r1", "b": "r2"})
        self.cluster.seed("a", "a0")
        self.cluster.seed("b", "b0")

    def test_prepared_transaction_blocks_until_primary_decision_is_recovered(self):
        tx = self.cluster.begin("tx")
        tx.put("a", "a1")
        tx.put("b", "b1")
        tx.prepare()

        self.cluster.crash("r1")
        with self.assertRaises(TransactionBlocked):
            self.cluster.read("b", self.cluster.clock)

        self.cluster.restart("r1")
        tx.abort()  # Recovery records an abort decision, then releases both locks.
        self.assertEqual(self.cluster.read("a", self.cluster.clock), "a0")
        self.assertEqual(self.cluster.read("b", self.cluster.clock), "b0")

    def test_primary_commit_is_decision_and_secondary_lock_resolves_lazily(self):
        tx = self.cluster.begin("tx")
        tx.put("a", "a1")
        tx.put("b", "b1")
        tx.prepare()
        commit_ts = tx.commit(pause_after_primary=True)

        self.assertIn("b", self.cluster.participants["r2"].locks)
        self.assertEqual(self.cluster.read("b", self.cluster.clock), "b1")
        self.assertNotIn("b", self.cluster.participants["r2"].locks)
        self.assertEqual(self.cluster.read("b", commit_ts - 1), "b0")

    def test_committed_decision_recovers_after_secondary_restarts(self):
        tx = self.cluster.begin("tx")
        tx.put("a", "a1")
        tx.put("b", "b1")
        tx.prepare()
        self.cluster.crash("r2")  # r2 already voted yes; its prepared state is retained.
        tx.commit()

        with self.assertRaises(ParticipantUnavailable):
            self.cluster.read("b", self.cluster.clock)
        self.cluster.restart("r2")
        self.assertEqual(self.cluster.read("b", self.cluster.clock), "b1")

    def test_primary_commit_cannot_be_rolled_back(self):
        tx = self.cluster.begin("tx")
        tx.put("a", "a1")
        tx.put("b", "b1")
        tx.prepare()
        tx.commit(pause_after_primary=True)
        with self.assertRaises(TransactionBlocked):
            tx.abort()
        self.assertEqual(self.cluster.read("b", self.cluster.clock), "b1")

    def test_prewrite_fails_and_cleans_up_when_participant_is_unavailable(self):
        tx = self.cluster.begin("tx")
        tx.put("a", "a1")
        tx.put("b", "b1")
        with self.assertRaises(ParticipantUnavailable):
            self.cluster.crash("r2")
            tx.prepare()
        self.cluster.restart("r2")
        tx.abort()


if __name__ == "__main__":
    unittest.main()
