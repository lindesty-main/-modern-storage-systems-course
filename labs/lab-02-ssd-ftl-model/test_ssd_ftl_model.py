import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ssd_ftl_model import FTLModel, _load_json, run_config, simulate_queue, validate_config


class FTLModelTests(unittest.TestCase):
    def setUp(self):
        self.geometry = {
            "page_bytes": 4096,
            "pages_per_block": 4,
            "logical_pages": 20,
            "physical_blocks": 8,
        }

    def test_gc_copies_live_pages_and_counts_write_amplification(self):
        model = FTLModel(self.geometry, allocation="fifo")
        trace = [0, 4, 8, 12, 16]
        result = model.run(trace)

        self.assertEqual(result["host_writes"], 5)
        self.assertEqual(result["gc_copied_pages"], 3)
        self.assertEqual(result["physical_programs"], 8)
        self.assertAlmostEqual(result["write_amplification"], 8 / 5, places=5)
        self.assertEqual(result["block_erases"], 1)
        self.assertEqual(result["free_blocks"], 2)
        self.assertTrue(model.check_invariants())

    def test_repeated_hot_page_overwrites_remain_collectable(self):
        model = FTLModel(self.geometry, allocation="least_worn")
        result = model.run([0] * 100)

        self.assertGreater(result["gc_runs"], 0)
        self.assertGreaterEqual(result["write_amplification"], 1.0)
        self.assertTrue(model.check_invariants())

    def test_workloads_are_reproducible_and_report_separate_policies(self):
        config = {
            "geometry": self.geometry,
            "workloads": [
                {"name": "hot", "kind": "hotset", "writes": 80, "hot_pages": 4, "seed": 17},
                {"name": "uniform", "kind": "uniform", "writes": 160, "seed": 17},
            ],
            "allocation_policies": ["fifo", "least_worn"],
            "queue": None,
        }
        validate_config(config)
        first = run_config(config)
        second = run_config(config)
        self.assertEqual(first, second)
        uniform = {row["allocation_policy"]: row for row in first["ftl"] if row["workload"] == "uniform"}
        self.assertLessEqual(
            uniform["least_worn"]["erase_count_spread"],
            uniform["fifo"]["erase_count_spread"],
        )
        self.assertGreaterEqual(uniform["fifo"]["write_amplification"], 1.0)

    def test_nonregular_config_path_is_rejected_before_open(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFCHR)
        with patch.object(Path, "lstat", return_value=metadata):
            with patch.object(Path, "open") as open_path:
                with self.assertRaises(ValueError):
                    _load_json(Path("synthetic-device-path.json"))
                open_path.assert_not_called()

    def test_invalid_overprovisioning_is_rejected(self):
        bad = {
            "geometry": {
                "page_bytes": 4096,
                "pages_per_block": 4,
                "logical_pages": 28,
                "physical_blocks": 8,
            },
            "workloads": [],
            "allocation_policies": ["fifo"],
            "queue": None,
        }
        with self.assertRaises(ValueError):
            validate_config(bad)


class QueueModelTests(unittest.TestCase):
    def test_fcfs_queue_and_network_rtt_are_separate(self):
        config = {
            "requests": 3,
            "interarrival_us": 10,
            "request_bytes": 0,
            "systems": {
                "local": {"lanes": 1, "fixed_us": 15, "us_per_mib": 0, "network_rtt_us": 0},
                "remote": {"lanes": 1, "fixed_us": 15, "us_per_mib": 0, "network_rtt_us": 100},
            },
        }
        result = simulate_queue(config)
        self.assertEqual(result["local"]["p50_us"], 20)
        self.assertEqual(result["local"]["p95_us"], 25)
        self.assertEqual(result["local"]["max_queue_wait_us"], 10)
        self.assertEqual(result["remote"]["p50_us"], 120)
        self.assertEqual(result["remote"]["p95_us"], 125)
        self.assertEqual(result["remote"]["max_queue_wait_us"], 10)


if __name__ == "__main__":
    unittest.main()
