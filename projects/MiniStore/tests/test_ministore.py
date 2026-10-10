#!/usr/bin/env python3
"""Black-box tests for the local MiniStore CLI; uses only temporary directories."""
import fcntl
import os
import pathlib
import select
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BINARY = ROOT / "build" / "ministore_test"
PRODUCTION_BINARY = ROOT / "build" / "ministore"


class MiniStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ministore-test-")
        self.addCleanup(self.tmp.cleanup)
        self.parent = pathlib.Path(self.tmp.name)
        self.db = self.parent / "db"
        self.init_store()

    def run_cli(self, *args, expected=0, env=None, binary=BINARY):
        if env is None:
            env = os.environ.copy()
            env.pop("MINISTORE_TEST_FAILPOINT", None)
        result = subprocess.run(
            [str(binary), *map(str, args)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
            env=env,
        )
        self.assertEqual(result.returncode, expected, (args, result.stdout, result.stderr))
        return result

    def init_store(self):
        return self.run_cli("init", "--dir", self.db)

    def command(self, name, *args, expected=0, env=None, binary=BINARY):
        return self.run_cli(
            name, "--dir", self.db, *args, expected=expected, env=env, binary=binary
        )

    def object_command(self, name, *args, payload=b"", expected=0, binary=BINARY):
        env = os.environ.copy()
        env.pop("MINISTORE_TEST_FAILPOINT", None)
        result = subprocess.run(
            [str(binary), name, "--dir", str(self.db), *map(str, args)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
            env=env,
        )
        self.assertEqual(result.returncode, expected, (args, result.stdout, result.stderr))
        return result

    def start_at_failpoint(self, name, *args):
        env = os.environ.copy()
        env["MINISTORE_TEST_FAILPOINT"] = name
        process = subprocess.Popen(
            [str(BINARY), *map(str, args)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        deadline = time.monotonic() + 5
        marker = f"TEST_FAILPOINT_REACHED:{name}"
        stderr_seen = ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([process.stderr], [], [], remaining)
            if not ready:
                break
            line = process.stderr.readline()
            stderr_seen += line
            if marker in stderr_seen:
                return process
            if process.poll() is not None:
                break
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        self.fail(
            f"failpoint {name!r} was not reached; returncode={process.returncode}, "
            f"stdout={stdout!r}, stderr={stderr_seen + stderr!r}"
        )

    def kill_at_failpoint(self, name, *args):
        process = self.start_at_failpoint(name, *args)
        process.kill()  # SIGKILL the user-space process; this is not a power-loss test.
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, -signal.SIGKILL, (stdout, stderr))
        return stdout, stderr

    def test_put_get_replace_delete_list_and_restart(self):
        self.command("put", "zeta", "last")
        self.command("put", "alpha", "first")
        self.command("put", "alpha", "updated")
        self.command("put", "empty", "")

        got = self.command("get", "alpha")
        self.assertEqual(got.stdout, "key\talpha\tvalue_hex\t75706461746564\n")
        self.assertEqual(
            self.command("list").stdout,
            "key\talpha\tvalue_hex\t75706461746564\n"
            "key\tempty\tvalue_hex\t\n"
            "key\tzeta\tvalue_hex\t6c617374\n",
        )
        self.command("delete", "alpha")
        self.command("get", "alpha", expected=3)
        self.assertEqual(
            self.command("list").stdout,
            "key\tempty\tvalue_hex\t\nkey\tzeta\tvalue_hex\t6c617374\n",
        )

        # Every CLI invocation reopens the store, so the next call proves restart recovery.
        self.command("put", "alpha", "again")
        self.assertEqual(
            self.command("get", "alpha").stdout,
            "key\talpha\tvalue_hex\t616761696e\n",
        )

    def test_object_api_put_get_overwrite_delete_and_list(self):
        first = b"old\x00payload\xff"
        self.assertEqual(
            self.object_command("object-put", "images", "zeta.bin", payload=first).stdout,
            b"OK seq=1 size=12\n",
        )
        self.object_command("object-put", "images", "alpha.bin", payload=b"alpha")
        replacement = b"replacement\x00"
        self.assertEqual(
            self.object_command(
                "object-put", "images", "zeta.bin", payload=replacement
            ).stdout,
            b"OK seq=3 size=12\n",
        )
        self.assertEqual(
            self.object_command("object-get", "images", "zeta.bin").stdout,
            replacement,
        )
        self.object_command("object-put", "documents", "zeta.bin", payload=b"other bucket")
        self.assertEqual(
            self.object_command("object-list", "images").stdout,
            b"object\talpha.bin\tsize\t5\nobject\tzeta.bin\tsize\t12\n",
        )
        self.assertEqual(
            self.object_command("object-list", "images", "z").stdout,
            b"object\tzeta.bin\tsize\t12\n",
        )
        self.object_command("object-delete", "images", "alpha.bin")
        self.assertEqual(
            self.object_command("object-get", "images", "alpha.bin", expected=3).stderr,
            b"not found: object not found: images/alpha.bin\n",
        )
        self.assertEqual(self.object_command("object-list", "images").stdout,
                         b"object\tzeta.bin\tsize\t12\n")
        self.object_command("object-delete", "images", "alpha.bin", expected=3)

    def test_object_api_rejects_invalid_names_and_oversized_payload_without_mutation(self):
        for bucket, key in (("bad.bucket", "key"), ("images", "../escape"), ("", "key")):
            with self.subTest(bucket=bucket, key=key):
                self.object_command("object-put", bucket, key, payload=b"no", expected=2)
        self.object_command(
            "object-put", "images", "large.bin", payload=b"x" * (1024 * 1024 + 1), expected=2
        )
        limit_payload = b"x" * (1024 * 1024)
        accepted = self.object_command("object-put", "images", "limit.bin", payload=limit_payload)
        self.assertEqual(accepted.stdout, b"OK seq=1 size=1048576\n")
        self.assertEqual(self.object_command("object-get", "images", "limit.bin").stdout,
                         limit_payload)
        self.assertEqual(self.object_command("object-list", "images").stdout,
                         b"object\tlimit.bin\tsize\t1048576\n")
        self.assertEqual(self.command("list").stdout, "")

    def test_empty_object_and_object_data_survive_compaction(self):
        self.object_command("object-put", "images", "empty.bin", payload=b"")
        data = b"before compaction\x00after"
        self.object_command("object-put", "images", "snapshot.bin", payload=data)
        self.command("compact")
        self.assertEqual(self.object_command("object-get", "images", "empty.bin").stdout, b"")
        self.assertEqual(self.object_command("object-get", "images", "snapshot.bin").stdout, data)
        self.assertEqual(
            self.object_command("object-list", "images").stdout,
            b"object\tempty.bin\tsize\t0\nobject\tsnapshot.bin\tsize\t23\n",
        )

    def test_m15_end_to_end_kv_object_compaction_and_restart(self):
        self.command("put", "manifest", "course-m15")
        self.object_command("object-put", "artifacts", "sample.bin", payload=b"M15\x00payload\xff")
        self.command("put", "manifest", "accepted")
        self.command("compact")

        self.assertEqual(
            self.command("get", "manifest").stdout,
            "key\tmanifest\tvalue_hex\t6163636570746564\n",
        )
        self.assertEqual(
            self.object_command("object-get", "artifacts", "sample.bin").stdout,
            b"M15\x00payload\xff",
        )
        self.assertEqual(
            self.command("list").stdout,
            "key\tmanifest\tvalue_hex\t6163636570746564\n",
        )
        self.assertEqual(
            self.object_command("object-list", "artifacts").stdout,
            b"object\tsample.bin\tsize\t12\n",
        )

    def test_object_namespace_does_not_collide_with_ordinary_kv_keys(self):
        self.object_command("object-put", "images", "photo.bin", payload=b"object bytes")
        self.command("put", "obj-images-photo.bin", "ordinary KV value")
        self.assertEqual(
            self.command("list").stdout,
            "key\tobj-images-photo.bin\tvalue_hex\t6f7264696e617279204b562076616c7565\n",
        )
        self.assertEqual(
            self.object_command("object-get", "images", "photo.bin").stdout,
            b"object bytes",
        )
        self.assertEqual(
            self.object_command("object-list", "images").stdout,
            b"object\tphoto.bin\tsize\t12\n",
        )
        self.command("get", "@obj:images:photo.bin", expected=2)

    def test_object_api_rejects_a_competing_process_while_database_is_locked(self):
        with (self.db / "LOCK").open("rb") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            failed = self.object_command("object-put", "images", "busy.bin", payload=b"x", expected=1)
            self.assertIn(b"already open", failed.stderr)
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        self.object_command("object-put", "images", "busy.bin", payload=b"x")
        self.assertEqual(self.object_command("object-get", "images", "busy.bin").stdout, b"x")

    def test_values_are_byte_preserving_and_list_output_is_unambiguous(self):
        value = "line one\nline two\t\\"
        self.command("put", "escaped", value)
        expected_hex = value.encode().hex()
        self.assertEqual(
            self.command("get", "escaped").stdout,
            f"key\tescaped\tvalue_hex\t{expected_hex}\n",
        )

    def test_init_never_overwrites_existing_directory(self):
        marker = self.parent / "already-there"
        marker.mkdir()
        keep = marker / "keep.txt"
        keep.write_text("unchanged")
        self.run_cli("init", "--dir", marker, expected=1)
        self.assertEqual(keep.read_text(), "unchanged")

    def test_invalid_keys_are_rejected_without_mutation(self):
        for key in ("", "has space", "../escape", "slash/name", "bad\nkey"):
            with self.subTest(key=key):
                self.command("put", key, "value", expected=2)
        self.assertEqual(self.command("list").stdout, "")

    def test_truncated_wal_header_and_payload_are_discarded(self):
        self.command("put", "durable", "before-tail")
        wal = self.db / "WAL"
        committed = wal.read_bytes()
        # A valid-looking second record header with only part of its value written.
        partial = b"MWR1" + (2).to_bytes(8, "little") + b"\x01" + (5).to_bytes(2, "little")
        partial += (10).to_bytes(4, "little") + b"otherpartial"
        wal.write_bytes(committed + partial)
        result = self.command("get", "durable")
        self.assertEqual(result.stdout, "key\tdurable\tvalue_hex\t6265666f72652d7461696c\n")
        self.assertEqual(wal.read_bytes(), committed)
        self.command("get", "other", expected=3)

        with wal.open("ab") as stream:
            stream.write(b"MWR1\x01\x02")
        self.command("get", "durable")
        self.assertEqual(wal.read_bytes(), committed)

    def test_sigkill_during_partial_wal_record_repairs_only_the_tail(self):
        self.command("put", "stable", "visible")
        wal = self.db / "WAL"
        committed = wal.read_bytes()

        stdout, stderr = self.kill_at_failpoint(
            "after_partial_wal_append", "put", "--dir", self.db, "uncommitted", "partial"
        )
        self.assertEqual(stdout, "")
        self.assertEqual(self.command("get", "stable").stdout,
                         "key\tstable\tvalue_hex\t76697369626c65\n")
        self.assertEqual(wal.read_bytes(), committed)
        self.command("get", "uncommitted", expected=3)

    def test_sigkill_after_wal_write_before_sync_can_replay_unacknowledged_write(self):
        stdout, stderr = self.kill_at_failpoint(
            "after_wal_write_before_fdatasync", "put", "--dir", self.db, "maybe", "visible"
        )
        self.assertEqual(stdout, "")
        # SIGKILL leaves the kernel running; a complete page-cache record can be replayed.
        self.assertEqual(
            self.command("get", "maybe").stdout,
            "key\tmaybe\tvalue_hex\t76697369626c65\n",
        )

    def test_sigkill_after_fdatasync_before_ack_recovers_the_committed_write(self):
        stdout, stderr = self.kill_at_failpoint(
            "after_wal_fdatasync_before_apply", "put", "--dir", self.db, "synced", "durable"
        )
        self.assertEqual(stdout, "")
        self.assertEqual(
            self.command("get", "synced").stdout,
            "key\tsynced\tvalue_hex\t64757261626c65\n",
        )

    def test_injected_wal_sync_error_reports_uncertain_outcome(self):
        env = os.environ.copy()
        env["MINISTORE_TEST_FAILPOINT"] = "inject_wal_fdatasync_error"
        failed = self.command("put", "uncertain", "written", expected=1, env=env)
        self.assertEqual(failed.stdout, "")
        self.assertIn("write outcome may be uncertain", failed.stderr)
        # This hook fails before fdatasync but after append; recovery sees page-cache data here.
        self.assertEqual(
            self.command("get", "uncertain").stdout,
            "key\tuncertain\tvalue_hex\t7772697474656e\n",
        )

    def test_test_failpoints_are_absent_from_the_normal_binary(self):
        env = os.environ.copy()
        env["MINISTORE_TEST_FAILPOINT"] = "after_wal_fdatasync_before_apply"
        result = self.command("put", "normal", "path", env=env, binary=PRODUCTION_BINARY)
        self.assertIn("OK seq=1", result.stdout)
        self.assertEqual(self.command("get", "normal").stdout,
                         "key\tnormal\tvalue_hex\t70617468\n")

    def test_complete_record_checksum_corruption_fails_closed(self):
        self.command("put", "key", "value")
        wal = self.db / "WAL"
        raw = bytearray(wal.read_bytes())
        raw[-1] ^= 0x80
        wal.write_bytes(raw)
        failed = self.run_cli("get", "--dir", self.db, "key", expected=1)
        self.assertIn("checksum", failed.stderr.lower())

    def test_sigkill_during_compaction_publication_recovers_snapshot_and_wal(self):
        for index, failpoint in enumerate((
            "after_table_rename_before_dir_fsync",
            "after_wal_rename_before_dir_fsync",
        )):
            with self.subTest(failpoint=failpoint):
                self.command("put", "keep", "new value")
                self.command("put", "gone", "old value")
                self.command("delete", "gone")
                old_wal = (self.db / "WAL").read_bytes()
                self.kill_at_failpoint(failpoint, "compact", "--dir", self.db)
                self.assertTrue((self.db / "TABLE.sst").is_file())
                if index == 0:
                    self.assertEqual((self.db / "WAL").read_bytes(), old_wal)
                else:
                    self.assertEqual((self.db / "WAL").read_bytes(), b"")
                expected_list = "key\tkeep\tvalue_hex\t6e65772076616c7565\n"
                if index == 1:
                    expected_list = "key\tafter\tvalue_hex\t7265636f76657279\n" + expected_list
                self.assertEqual(self.command("list").stdout, expected_list)
                self.command("put", "after", "recovery")
                self.assertIn("7265636f76657279", self.command("get", "after").stdout)
                # Leave each subtest with a clean database for the next phase.
                self.command("compact")

    def test_compaction_reopens_and_removes_tombstones(self):
        self.command("put", "keep", "value")
        self.command("put", "gone", "old")
        self.command("delete", "gone")
        old_wal = (self.db / "WAL").read_bytes()
        self.command("compact")
        self.assertTrue((self.db / "TABLE.sst").is_file())
        self.assertEqual((self.db / "WAL").stat().st_size, 0)
        self.assertEqual(self.command("list").stdout, "key\tkeep\tvalue_hex\t76616c7565\n")
        # A crash between publishing TABLE.sst and rotating WAL leaves the old WAL.
        # Replaying it must not roll the compacted snapshot backwards.
        (self.db / "WAL").write_bytes(old_wal)
        self.assertEqual(self.command("list").stdout, "key\tkeep\tvalue_hex\t76616c7565\n")
        self.command("put", "new", "after compact")
        self.assertIn("616674657220636f6d70616374", self.command("get", "new").stdout)

    def test_database_lock_is_exclusive(self):
        with (self.db / "LOCK").open("rb") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            failed = self.run_cli("list", "--dir", self.db, expected=1)
            self.assertIn("already open", failed.stderr)
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    def test_compacted_table_checksum_corruption_fails_closed(self):
        self.command("put", "key", "value")
        self.command("compact")
        table = self.db / "TABLE.sst"
        raw = bytearray(table.read_bytes())
        raw[-1] ^= 1
        table.write_bytes(raw)
        failed = self.run_cli("get", "--dir", self.db, "key", expected=1)
        self.assertIn("checksum", failed.stderr.lower())

    def test_non_regular_wal_is_rejected_without_blocking(self):
        wal = self.db / "WAL"
        wal.unlink()
        os.mkfifo(wal)
        self.run_cli("list", "--dir", self.db, expected=1)

    def test_symlink_database_and_symlink_wal_are_rejected(self):
        link = self.parent / "db-link"
        link.symlink_to(self.db, target_is_directory=True)
        self.run_cli("list", "--dir", link, expected=1)

        outside = self.parent / "outside"
        outside.write_bytes(b"do not modify")
        wal = self.db / "WAL"
        wal.unlink()
        wal.symlink_to(outside)
        self.run_cli("put", "--dir", self.db, "key", "value", expected=1)
        self.assertEqual(outside.read_bytes(), b"do not modify")


if __name__ == "__main__":
    unittest.main(verbosity=2)
