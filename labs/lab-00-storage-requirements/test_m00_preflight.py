#!/usr/bin/env python3
"""Tests for the read-only M00 preflight and requirement-card validator."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import m00_preflight


HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / "requirement-card.example.json"


class RequirementCardTests(unittest.TestCase):
    def setUp(self):
        self.card = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def test_example_card_is_complete(self):
        self.assertEqual(m00_preflight.validate_card(self.card), [])

    def test_schema_version_must_be_an_integer(self):
        self.card["schema_version"] = 1.0
        errors = m00_preflight.validate_card(self.card)
        self.assertTrue(any("schema_version" in error for error in errors))

    def test_read_write_percentages_must_sum_to_100(self):
        self.card["workload"]["write_percent"] = 30
        errors = m00_preflight.validate_card(self.card)
        self.assertTrue(any("sum to 100" in error for error in errors))

    def test_invalid_percent_and_nonpositive_latency_are_rejected(self):
        self.card["workload"]["read_percent"] = 101
        self.card["slo"]["p99_latency_ms"] = 0
        errors = m00_preflight.validate_card(self.card)
        self.assertTrue(any("read_percent" in error for error in errors))
        self.assertTrue(any("p99_latency_ms" in error for error in errors))

    def test_nonfinite_numbers_and_unexpected_access_pattern_are_rejected(self):
        self.card["data"]["initial_gib"] = float("inf")
        self.card["workload"]["access_pattern"] = []
        errors = m00_preflight.validate_card(self.card)
        self.assertTrue(any("initial_gib" in error and "finite" in error for error in errors))
        self.assertTrue(any("access_pattern" in error for error in errors))

    def test_failure_model_and_option_comparison_are_required(self):
        self.card["failure_model"]["in_scope"] = []
        self.card["candidate_options"] = self.card["candidate_options"][:1]
        errors = m00_preflight.validate_card(self.card)
        self.assertTrue(any("failure_model.in_scope" in error for error in errors))
        self.assertTrue(any("at least two" in error for error in errors))

    def test_json_card_cli_reports_invalid_file_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            card_path = Path(directory) / "card.json"
            invalid = dict(self.card)
            invalid["schema_version"] = 99
            original = json.dumps(invalid, ensure_ascii=False, indent=2) + "\n"
            card_path.write_text(original, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(HERE / "m00_preflight.py"), "validate", str(card_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("schema_version", result.stderr)
            self.assertEqual(card_path.read_text(encoding="utf-8"), original)

    def test_symlink_card_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "card.json"
            link.symlink_to(EXAMPLE)
            result = subprocess.run(
                [sys.executable, str(HERE / "m00_preflight.py"), "validate", str(link)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("symbolic-link", result.stderr)
            self.assertTrue(EXAMPLE.is_file())


class ReadOnlyCheckTests(unittest.TestCase):
    def test_environment_report_is_read_only_and_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            before = set(Path(directory).iterdir())
            result = subprocess.run(
                [sys.executable, str(HERE / "m00_preflight.py"), "check", "--json"],
                cwd=directory,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn("python", report)
            self.assertIn("os", report)
            self.assertIn("kernel", report)
            self.assertIn("tools", report)
            self.assertEqual(set(Path(directory).iterdir()), before)


if __name__ == "__main__":
    unittest.main()
