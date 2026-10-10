import subprocess
import sys
import unittest
from pathlib import Path

import bench


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        values = [50, 10, 40, 20, 30]
        self.assertEqual(bench.percentile(values, 50), 30)
        self.assertEqual(bench.percentile(values, 95), 50)
        self.assertEqual(bench.percentile(values, 99), 50)

    def test_rejects_empty_or_invalid_percentiles(self):
        with self.assertRaises(ValueError):
            bench.percentile([], 50)
        with self.assertRaises(ValueError):
            bench.percentile([1], 0)


class FileBenchmarkTests(unittest.TestCase):
    def test_fixed_seed_repeats_verify_data_and_report_tail_latency(self):
        report = bench.run_file_benchmark(
            mode="mixed",
            blocks=16,
            block_size=512,
            operations=100,
            repetitions=2,
            seed=12345,
            write_percent=25,
        )
        self.assertTrue(report["correctness"]["passed"])
        self.assertEqual(report["config"]["seed"], 12345)
        self.assertEqual(len(report["workload_trace_sha256"]), 64)
        self.assertEqual(len(report["runs"]), 2)
        self.assertEqual(
            report["runs"][0]["final_sha256"],
            report["runs"][1]["final_sha256"],
        )
        for run in report["runs"]:
            latency = run["latency_ns"]
            self.assertEqual(run["operations"], 100)
            self.assertLessEqual(latency["p50"], latency["p95"])
            self.assertLessEqual(latency["p95"], latency["p99"])
            self.assertGreater(run["operations_per_second"], 0)

    def test_different_seed_changes_generated_data(self):
        common = dict(
            mode="randread",
            blocks=8,
            block_size=512,
            operations=12,
            repetitions=1,
            write_percent=0,
        )
        first = bench.run_file_benchmark(seed=1, **common)
        second = bench.run_file_benchmark(seed=2, **common)
        self.assertNotEqual(
            first["runs"][0]["final_sha256"],
            second["runs"][0]["final_sha256"],
        )


class CacheSimulationTests(unittest.TestCase):
    def test_policies_return_identical_values_for_same_trace(self):
        report = bench.run_cache_simulation(
            keys=32,
            operations=500,
            capacity=4,
            seed=20261009,
        )
        self.assertTrue(report["correctness"]["passed"])
        self.assertEqual(len(set(report["result_sha256"].values())), 1)
        for policy in ("none", "lru", "fifo"):
            result = report["policies"][policy]
            self.assertEqual(result["hits"] + result["misses"], 500)
            self.assertLessEqual(result["resident_entries"], 4)

    def test_lru_and_fifo_have_expected_distinct_eviction_behavior(self):
        trace = [1, 2, 1, 3, 1]
        lru = bench.simulate_cache_trace(trace, "lru", 2)
        fifo = bench.simulate_cache_trace(trace, "fifo", 2)
        self.assertEqual(lru["hits"], 2)
        self.assertEqual(fifo["hits"], 1)
        self.assertEqual(lru["result_sha256"], fifo["result_sha256"])

    def test_no_cache_is_all_misses_and_policy_validation(self):
        report = bench.simulate_cache_trace([1, 1, 2, 1], "none", 3)
        self.assertEqual(report["hits"], 0)
        self.assertEqual(report["misses"], 4)
        with self.assertRaises(ValueError):
            bench.simulate_cache_trace([1], "random", 1)

    def test_file_cli_rejects_external_path_arguments(self):
        script = Path(bench.__file__)
        result = subprocess.run(
            [sys.executable, str(script), "file", "--filename", "/dev/zero"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
