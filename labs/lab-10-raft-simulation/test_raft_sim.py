import unittest

from raft_sim import RaftCluster


class ElectionTests(unittest.TestCase):
    def test_candidate_wins_only_after_majority_votes(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.partition(("n1",), ("n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        self.assertEqual(cluster.leaders(), [])

        cluster.heal()
        cluster.start_election("n1")
        cluster.run()
        self.assertEqual([node.id for node in cluster.leaders()], ["n1"])
        self.assertEqual(cluster.nodes["n1"].current_term, 2)

    def test_stale_leader_steps_down_after_higher_term_partition_wins(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        cluster.partition(("n1",), ("n2", "n3"))

        cluster.start_election("n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n2"].role, "leader")
        self.assertEqual(cluster.nodes["n1"].role, "leader")  # isolated, stale view

        cluster.heal()
        cluster.replicate_from("n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].role, "follower")
        self.assertGreater(cluster.nodes["n1"].current_term, 1)


class ReplicationAndSafetyTests(unittest.TestCase):
    def test_entry_is_committed_and_applied_only_after_majority(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        cluster.partition(("n1",), ("n2", "n3"))

        index = cluster.propose("write:k=v", leader_id="n1")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].commit_index, 0)
        self.assertEqual(cluster.nodes["n1"].last_applied, 0)
        self.assertEqual(cluster.nodes["n1"].last_index, index)

        cluster.heal()
        cluster.replicate_from("n1")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].commit_index, index)
        self.assertEqual(cluster.nodes["n2"].commit_index, index)
        self.assertEqual(cluster.nodes["n3"].commit_index, index)
        self.assertEqual(cluster.nodes["n3"].applied_commands, ["write:k=v"])
        cluster.assert_safety()

    def test_uncommitted_suffix_is_replaced_by_new_majority_leader(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        cluster.partition(("n1",), ("n2", "n3"))
        old_index = cluster.propose("uncommitted", leader_id="n1")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].commit_index, 0)

        cluster.start_election("n2")
        cluster.run()
        new_index = cluster.propose("chosen", leader_id="n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n2"].commit_index, new_index)

        cluster.heal()
        cluster.replicate_from("n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].entry_at(old_index).value, "chosen")
        self.assertEqual(cluster.nodes["n1"].commit_index, new_index)
        cluster.assert_safety()

    def test_old_term_entry_needs_current_term_entry_to_commit(self):
        cluster = RaftCluster(("n1", "n2", "n3", "n4", "n5"))
        cluster.start_election("n1")
        cluster.run()
        cluster.partition(("n1", "n2"), ("n3", "n4", "n5"))
        old_index = cluster.propose("term-one", leader_id="n1")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].commit_index, 0)

        cluster.heal()
        cluster.start_election("n1")  # deterministic election-timeout event
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].current_term, 2)
        self.assertEqual(cluster.nodes["n1"].role, "leader")
        self.assertEqual(cluster.nodes["n1"].commit_index, 0)
        self.assertEqual(cluster.nodes["n1"].entry_at(old_index).term, 1)

        current_index = cluster.propose("term-two", leader_id="n1")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].commit_index, current_index)
        self.assertEqual(cluster.nodes["n1"].commit_index, old_index + 1)
        cluster.assert_safety()

    def test_stale_candidate_cannot_get_vote_from_up_to_date_voter(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        cluster.crash("n3")
        committed = cluster.propose("durable-majority-entry", leader_id="n1")
        cluster.run()
        self.assertEqual(cluster.nodes["n2"].commit_index, committed)
        cluster.crash("n1")
        cluster.restart("n3")

        cluster.start_election("n3")
        cluster.run()
        self.assertEqual(cluster.nodes["n3"].role, "candidate")
        self.assertEqual(cluster.nodes["n2"].voted_for, None)
        self.assertEqual(cluster.leaders(), [])

        cluster.start_election("n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n2"].role, "leader")
        self.assertEqual(cluster.nodes["n2"].entry_at(committed).value,
                         "durable-majority-entry")
        cluster.assert_safety()

    def test_leader_failure_allows_surviving_majority_to_elect_and_replicate(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        cluster.propose("before-crash", leader_id="n1")
        cluster.run()
        cluster.crash("n1")

        cluster.start_election("n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n2"].role, "leader")
        index = cluster.propose("after-crash", leader_id="n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n2"].commit_index, index)

        cluster.restart("n1")
        cluster.replicate_from("n2")
        cluster.run()
        self.assertEqual(cluster.nodes["n1"].applied_commands,
                         ["before-crash", "after-crash"])
        cluster.assert_safety()


class SnapshotAndMembershipTests(unittest.TestCase):
    def test_lagging_node_installs_snapshot_then_catches_up(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        cluster.crash("n3")
        for command in ("a", "b", "c"):
            cluster.propose(command, leader_id="n1")
            cluster.run()
        snapshot_index = cluster.snapshot("n1")
        self.assertEqual(snapshot_index, 3)

        cluster.restart("n3")
        cluster.replicate_from("n1")
        cluster.run()
        self.assertEqual(cluster.nodes["n3"].snapshot_index, snapshot_index)
        self.assertEqual(cluster.nodes["n3"].applied_commands, ["a", "b", "c"])
        self.assertTrue(any("snapshot" in event.lower() for event in cluster.trace))
        cluster.assert_safety()

    def test_membership_change_uses_joint_quorum_then_stabilizes(self):
        cluster = RaftCluster(("n1", "n2", "n3"))
        cluster.start_election("n1")
        cluster.run()
        cluster.add_node("n4")
        cluster.run()

        cluster.change_membership(("n1", "n2", "n4"), leader_id="n1")
        self.assertEqual(cluster.joint_voters, None)
        self.assertEqual(cluster.voters, {"n1", "n2", "n4"})
        self.assertEqual(cluster.nodes["n4"].commit_index,
                         cluster.nodes["n1"].commit_index)
        cluster.assert_safety()

    def test_final_configuration_can_commit_with_new_quorum_after_joint_phase(self):
        cluster = RaftCluster(("a", "b", "c", "d", "e"), voters=("a", "b", "c"))
        cluster.start_election("a")
        cluster.run()
        cluster.begin_membership_change(("a", "d", "e"), leader_id="a")
        self.assertEqual(cluster.joint_voters, ({"a", "b", "c"}, {"a", "d", "e"}))

        # After C_old,new commits, C_new can complete using its own quorum.
        cluster.partition(("a", "d", "e"), ("b", "c"))
        cluster.finalize_membership(leader_id="a")
        self.assertEqual(cluster.joint_voters, None)
        self.assertEqual(cluster.voters, {"a", "d", "e"})
        cluster.assert_safety()

    def test_nodes_that_missed_joint_phase_catch_up_after_final_configuration(self):
        cluster = RaftCluster(("a", "b", "c", "d", "e"), voters=("a", "b", "c"))
        cluster.start_election("a")
        cluster.run()

        # a,b form an old majority and a,d form a new majority; c,e miss C_old,new.
        cluster.partition(("a", "b", "d"), ("c", "e"))
        joint_index = cluster.begin_membership_change(("a", "d", "e"), leader_id="a")
        self.assertEqual(cluster.nodes["a"].commit_index, joint_index)
        self.assertEqual(cluster.nodes["c"].commit_index, 0)
        self.assertEqual(cluster.nodes["e"].commit_index, 0)

        # C_new obtains its own majority while b,c remain partitioned; then
        # lagging nodes receive both config entries and the committed prefix.
        cluster.heal()
        cluster.partition(("a", "d", "e"), ("b", "c"))
        cluster.finalize_membership(leader_id="a")
        cluster.heal()
        cluster.replicate_from("a")
        cluster.run()
        self.assertEqual(cluster.voters, {"a", "d", "e"})
        self.assertEqual(cluster.nodes["b"].commit_index, cluster.nodes["a"].commit_index)
        self.assertEqual(cluster.nodes["c"].commit_index, cluster.nodes["a"].commit_index)
        cluster.assert_safety()

    def test_joint_configuration_requires_majority_of_both_sets(self):
        cluster = RaftCluster(("a", "b", "c", "d", "e"), voters=("a", "b", "c"))
        cluster.start_election("a")
        cluster.run()
        # {a,b,c} is an old-config majority, but a is the only reachable
        # voter in the new config; joint mode must not finalize.
        cluster.partition(("a", "b", "c"), ("d", "e"))
        with self.assertRaises(RuntimeError):
            cluster.change_membership(("a", "d", "e"), leader_id="a")
        self.assertEqual(cluster.joint_voters, ({"a", "b", "c"}, {"a", "d", "e"}))
        self.assertEqual(cluster.nodes["a"].commit_index,
                         cluster.nodes["a"].last_index - 1)

        cluster.heal()
        cluster.finalize_membership(leader_id="a")
        self.assertEqual(cluster.joint_voters, None)
        self.assertEqual(cluster.voters, {"a", "d", "e"})
        cluster.assert_safety()


if __name__ == "__main__":
    unittest.main()
