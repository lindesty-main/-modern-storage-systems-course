import unittest

from ceph_failure_sim import place_replicas, simulate


class PlacementTests(unittest.TestCase):
    def test_replicas_are_placed_in_distinct_failure_domains(self):
        domains = ("rack-a", "rack-b", "rack-c", "rack-d")
        placement = place_replicas("object-7", domains, replicas=3, seed=41)

        self.assertEqual(len(placement), 3)
        self.assertEqual(len(set(placement)), 3)

    def test_one_failed_domain_degrades_and_rebuilds_into_spare_domain(self):
        result = simulate(
            objects=50,
            domains=("rack-a", "rack-b", "rack-c", "rack-d"),
            replicas=3,
            failed_domains=("rack-a",),
            min_size=2,
            foreground_iops=60,
            rebuild_iops=10,
            capacity_iops=100,
            seed=41,
        )

        self.assertEqual(result.readable_objects, 50)
        self.assertEqual(result.min_size_objects, 50)
        self.assertGreater(result.degraded_objects, 0)
        self.assertEqual(result.missing_replica_copies, result.degraded_objects)
        self.assertEqual(result.rebuildable_copies, result.missing_replica_copies)
        self.assertEqual(result.unrebuildable_copies, 0)

    def test_object_with_no_surviving_copy_is_not_rebuilt_from_nothing(self):
        domains = ("rack-a", "rack-b", "rack-c", "rack-d")
        failed = place_replicas("object-00000000", domains, replicas=3, seed=3)
        result = simulate(
            objects=1,
            domains=domains,
            replicas=3,
            failed_domains=failed,
            min_size=2,
            foreground_iops=50,
            rebuild_iops=10,
            capacity_iops=100,
            seed=3,
        )

        self.assertEqual(result.unreadable_objects, 1)
        self.assertEqual(result.rebuildable_copies, 0)
        self.assertEqual(result.unrebuildable_copies, 3)

    def test_losing_all_replicas_cannot_rebuild_lost_object_data(self):
        result = simulate(
            objects=12,
            domains=("rack-a", "rack-b", "rack-c"),
            replicas=3,
            failed_domains=("rack-a", "rack-b", "rack-c"),
            min_size=2,
            foreground_iops=50,
            rebuild_iops=10,
            capacity_iops=100,
            seed=3,
        )

        self.assertEqual(result.readable_objects, 0)
        self.assertEqual(result.rebuildable_copies, 0)
        self.assertEqual(result.unrebuildable_copies, result.missing_replica_copies)

    def test_no_spare_domain_means_degraded_copies_cannot_be_rebuilt(self):
        result = simulate(
            objects=40,
            domains=("rack-a", "rack-b", "rack-c"),
            replicas=3,
            failed_domains=("rack-a",),
            min_size=2,
            foreground_iops=50,
            rebuild_iops=10,
            capacity_iops=100,
            seed=7,
        )

        self.assertGreater(result.missing_replica_copies, 0)
        self.assertEqual(result.rebuildable_copies, 0)
        self.assertEqual(result.unrebuildable_copies, result.missing_replica_copies)

    def test_rebuild_throttle_exposes_duration_and_capacity_tradeoff(self):
        common = dict(
            objects=60,
            domains=("rack-a", "rack-b", "rack-c", "rack-d"),
            replicas=3,
            failed_domains=("rack-a",),
            min_size=2,
            foreground_iops=75,
            capacity_iops=100,
            seed=19,
        )
        slow = simulate(rebuild_iops=10, **common)
        fast = simulate(rebuild_iops=30, **common)

        self.assertEqual(slow.combined_utilization_pct, 85.0)
        self.assertEqual(fast.combined_utilization_pct, 105.0)
        self.assertEqual(slow.overload_iops, 0)
        self.assertEqual(fast.overload_iops, 5)
        self.assertAlmostEqual(
            slow.nominal_rebuild_seconds,
            fast.nominal_rebuild_seconds * 3,
        )

    def test_rejects_replica_count_larger_than_failure_domain_count(self):
        with self.assertRaises(ValueError):
            place_replicas("object-1", ("rack-a", "rack-b"), replicas=3, seed=1)


if __name__ == "__main__":
    unittest.main()
