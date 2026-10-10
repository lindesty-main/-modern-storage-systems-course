import json
import tempfile
import unittest
from pathlib import Path

from observability_demo import analyze_observation, parse_prometheus, run_demo


class PrometheusParserTests(unittest.TestCase):
    def test_parses_comments_labels_and_numeric_values(self):
        metrics = parse_prometheus(
            '# HELP storage_ready readiness\n'
            'storage_ready{role="primary",pool="lab"} 1\n'
            'storage_bytes 1.25e6\n'
        )
        self.assertEqual(metrics[("storage_ready", (("pool", "lab"), ("role", "primary")))], 1)
        self.assertEqual(metrics[("storage_bytes", ())], 1_250_000)

    def test_rejects_duplicate_and_malformed_samples(self):
        with self.assertRaises(ValueError):
            parse_prometheus("storage_ready 1\nstorage_ready 0\n")
        with self.assertRaises(ValueError):
            parse_prometheus('storage_ready{role="primary"} not-a-number\n')


class DiagnosisTests(unittest.TestCase):
    def _demo_report(self, scenario):
        with tempfile.TemporaryDirectory(prefix="lab13-test-") as parent:
            result = run_demo(scenario=scenario, requests=12, seed=42, output_parent=parent)
            artifact_dir = Path(result["artifact_dir"])
            logs = (artifact_dir / "localhost.jsonl").read_text(encoding="utf-8")
            metrics = (artifact_dir / "synthetic.prom").read_text(encoding="utf-8")
            traces = (artifact_dir / "traces.jsonl").read_text(encoding="utf-8")
            report = analyze_observation(logs, metrics, traces, now=result["observed_at"])
            return result, report, logs, metrics, traces

    @staticmethod
    def _codes(report):
        return {finding["code"] for finding in report["findings"]}

    def test_healthy_localhost_run_has_correlated_logs_metrics_and_traces(self):
        result, report, logs, metrics, traces = self._demo_report("healthy")
        self.assertEqual(result["requests_sent"], 12)
        self.assertEqual(report["telemetry"]["observed_log_events"], 12)
        self.assertEqual(report["telemetry"]["metric_request_count"], 12)
        self.assertEqual(report["telemetry"]["trace_correlated_events"], 12)
        self.assertEqual(self._codes(report), set())
        first_log = json.loads(logs.splitlines()[0])
        self.assertEqual(first_log["peer_ip"], "127.0.0.1")
        self.assertIn(first_log["trace_id"], traces)
        self.assertIn("storage_requests_total", metrics)

    def test_slow_backend_is_identified_by_tail_latency_not_average_only(self):
        _, report, *_ = self._demo_report("latency")
        self.assertIn("P95_LATENCY_OVER_BUDGET", self._codes(report))
        self.assertGreater(report["latency"]["p95_ms"], report["latency"]["p50_ms"])

    def test_capacity_pressure_is_distinguished_from_process_liveness(self):
        _, report, *_ = self._demo_report("capacity")
        self.assertIn("CAPACITY_HIGH", self._codes(report))
        self.assertTrue(report["health"]["live"])
        self.assertTrue(report["health"]["ready"])
        self.assertGreater(report["capacity"]["utilization"], 0.9)

    def test_backup_age_and_restore_test_are_compared_with_rpo_rto(self):
        _, report, *_ = self._demo_report("backup")
        codes = self._codes(report)
        self.assertIn("RPO_OBJECTIVE_MISSED", codes)
        self.assertIn("RTO_TEST_OVER_BUDGET", codes)
        self.assertIn("RESTORE_TEST_STALE", codes)

    def test_request_errors_change_readiness_without_claiming_process_death(self):
        _, report, *_ = self._demo_report("errors")
        self.assertIn("HIGH_ERROR_RATE", self._codes(report))
        self.assertFalse(report["health"]["ready"])
        self.assertTrue(report["health"]["live"])

    def test_missing_metric_samples_are_unknown_not_zero(self):
        _, report, *_ = self._demo_report("metrics-gap")
        self.assertIn("METRICS_INCOMPLETE", self._codes(report))
        self.assertEqual(report["telemetry"]["observed_log_events"], 12)
        self.assertIsNone(report["telemetry"]["metric_request_count"])

    def test_exported_metric_series_have_bounded_labels_not_object_keys(self):
        _, _, logs, metrics, _ = self._demo_report("healthy")
        self.assertNotIn("synthetic-", metrics)
        self.assertLessEqual(len(parse_prometheus(metrics)), 32)
        self.assertIn('"operation":"get"', logs)


if __name__ == "__main__":
    unittest.main()
