import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from security_audit import (
    LabError,
    decode_record,
    encode_record,
    read_record,
    run_demo,
    write_record,
)


class RecordValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="m14-test-")
        self.root = Path(self.temp.name) / "vault"
        self.root.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_directory_and_file_modes_and_valid_round_trip(self):
        write_record(self.root, "tenant-a", "object-1", b"local demo bytes")
        tenant_dir = self.root / "tenant-a"
        record = tenant_dir / "object-1.record"
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(tenant_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertEqual(read_record(self.root, "tenant-a", "tenant-a", "object-1"), b"local demo bytes")

    def test_authorization_denies_other_tenant_before_opening_record(self):
        with self.assertRaises(LabError) as caught:
            read_record(self.root, "tenant-b", "tenant-a", "missing")
        self.assertEqual(caught.exception.code, "AUTHORIZATION_DENIED")

    def test_path_components_reject_traversal_and_invalid_characters(self):
        for tenant, key in (("../outside", "key"), ("tenant-a", "../outside"), ("tenant-a", "bad/key")):
            with self.subTest(tenant=tenant, key=key), self.assertRaises(LabError) as caught:
                write_record(self.root, tenant, key, b"x")
            self.assertEqual(caught.exception.code, "INVALID_COMPONENT")

    def test_payload_limit_is_enforced_before_filesystem_write(self):
        with self.assertRaises(LabError) as caught:
            write_record(self.root, "tenant-a", "too-large", b"x" * 4097)
        self.assertEqual(caught.exception.code, "INPUT_TOO_LARGE")
        self.assertFalse((self.root / "tenant-a").exists())

    def test_corrupt_digest_and_metadata_tampering_are_rejected(self):
        encoded = encode_record("tenant-a", "object-1", b"payload")
        record = json.loads(encoded)
        record["sha256"] = "0" * 64
        with self.assertRaises(LabError) as caught:
            decode_record(json.dumps(record).encode(), "tenant-a", "object-1")
        self.assertEqual(caught.exception.code, "CHECKSUM_MISMATCH")

        record = json.loads(encoded)
        record["tenant"] = "tenant-b"
        with self.assertRaises(LabError) as caught:
            decode_record(json.dumps(record).encode(), "tenant-a", "object-1")
        # Authorization is checked before checksum detail to avoid a validation oracle.
        self.assertEqual(caught.exception.code, "AUTHORIZATION_DENIED")

    def test_malformed_or_unexpected_record_fields_fail_closed(self):
        for raw in (
            b"not-json",
            b"[]",
            b'{"version": 1, "extra": true}',
            b'{"version":1,"version":1,"tenant":"tenant-a","key":"object-1","payload_b64":"","sha256":"'
            b'0000000000000000000000000000000000000000000000000000000000000000"}',
        ):
            with self.subTest(raw=raw), self.assertRaises(LabError) as caught:
                decode_record(raw, "tenant-a", "object-1")
            self.assertEqual(caught.exception.code, "INVALID_RECORD")

    def test_symlink_is_rejected_without_following_target(self):
        write_record(self.root, "tenant-a", "real", b"secret demo data")
        alias = self.root / "tenant-a" / "alias.record"
        alias.symlink_to("real.record")
        with self.assertRaises(LabError) as caught:
            read_record(self.root, "tenant-a", "tenant-a", "alias")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")

    def test_write_refuses_to_overwrite_existing_record(self):
        write_record(self.root, "tenant-a", "object-1", b"first")
        with self.assertRaises(LabError) as caught:
            write_record(self.root, "tenant-a", "object-1", b"replacement")
        self.assertEqual(caught.exception.code, "ALREADY_EXISTS")
        self.assertEqual(read_record(self.root, "tenant-a", "tenant-a", "object-1"), b"first")


class OfflineAuditTests(unittest.TestCase):
    def test_demo_reports_successful_permissions_validation_and_error_audits(self):
        report = run_demo()
        self.assertEqual(report["result"], "PASS")
        self.assertTrue(report["offline"])
        self.assertFalse(report["network_used"])
        self.assertFalse(report["device_paths_opened"])
        self.assertGreaterEqual(len(report["checks"]), 7)
        self.assertTrue(all(check["status"] == "PASS" for check in report["checks"]))
        self.assertEqual(
            report["error_audit"]["expected_rejections"],
            {
                "ALREADY_EXISTS": 1,
                "AUTHORIZATION_DENIED": 1,
                "CHECKSUM_MISMATCH": 1,
                "INVALID_COMPONENT": 1,
                "UNSAFE_PATH": 1,
            },
        )
        serialized = json.dumps(report)
        self.assertNotIn("secret demo data", serialized)
        self.assertNotIn(tempfile.gettempdir(), serialized)
        self.assertFalse(report["error_audit"]["raw_input_logged"])

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"), "requires POSIX no-follow directory opens")
    def test_private_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="m14-root-") as parent:
            actual = Path(parent) / "actual"
            actual.mkdir(mode=0o700)
            alias = Path(parent) / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(LabError) as caught:
                read_record(alias, "tenant-a", "tenant-a", "anything")
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")


if __name__ == "__main__":
    unittest.main()
