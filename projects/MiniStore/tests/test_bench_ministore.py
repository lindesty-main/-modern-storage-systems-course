#!/usr/bin/env python3
"""Tests for the bounded, temporary-directory MiniStore CLI benchmark."""
import unittest

from bench_ministore import percentile, run_benchmark, validate_config


class MiniStoreBenchmarkTest(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        self.assertEqual(percentile([8, 1, 5, 2], 50), 2)
        self.assertEqual(percentile([8, 1, 5, 2], 95), 8)

    def test_invalid_config_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_config(operations=0, key_count=1, value_size=32,
                           repetitions=1, write_percent=50, seed=0)
        with self.assertRaises(ValueError):
            validate_config(operations=1, key_count=1, value_size=1,
                           repetitions=1, write_percent=50, seed=0)

    def test_end_to_end_run_reports_correctness_and_latency_without_user_paths(self):
        report = run_benchmark(
            operations=8,
            key_count=4,
            value_size=64,
            repetitions=1,
            write_percent=50,
            seed=17,
        )
        self.assertTrue(report["correctness"]["passed"])
        self.assertEqual(report["config"]["operations_per_run"], 8)
        self.assertEqual(report["config"]["seed"], 17)
        self.assertEqual(report["runs"][0]["operation_count"], 8)
        self.assertIn("p99_ns", report["runs"][0]["latency_ns"])
        self.assertEqual(len(report["runs"][0]["final_values_sha256"]), 64)
        self.assertEqual(report["io_contract"]["concurrency"], "single process, QD=1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
