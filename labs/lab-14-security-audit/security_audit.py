#!/usr/bin/env python3
"""Offline, temporary-directory security checks for the M14 storage lesson."""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable


MAX_PAYLOAD_BYTES = 4096
MAX_RECORD_BYTES = 16 * 1024
_TENANT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class LabError(Exception):
    """A deliberately low-detail error suitable for the local audit summary."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _validate_component(value: str, *, tenant: bool = False) -> None:
    pattern = _TENANT if tenant else _KEY
    if not isinstance(value, str) or value in {".", ".."} or not pattern.fullmatch(value):
        raise LabError("INVALID_COMPONENT")


def _canonical_body(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def encode_record(tenant: str, key: str, payload: bytes) -> bytes:
    """Encode one bounded local record with an unkeyed corruption checksum."""
    _validate_component(tenant, tenant=True)
    _validate_component(key)
    if not isinstance(payload, bytes):
        raise LabError("INVALID_INPUT")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise LabError("INPUT_TOO_LARGE")
    body = {
        "key": key,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "tenant": tenant,
        "version": 1,
    }
    envelope = dict(body)
    envelope["sha256"] = hashlib.sha256(_canonical_body(body)).hexdigest()
    encoded = _canonical_body(envelope)
    if len(encoded) > MAX_RECORD_BYTES:
        raise LabError("INPUT_TOO_LARGE")
    return encoded


def _authorize(principal_tenant: str, resource_tenant: str) -> None:
    _validate_component(principal_tenant, tenant=True)
    _validate_component(resource_tenant, tenant=True)
    if not hmac.compare_digest(principal_tenant.encode("ascii"), resource_tenant.encode("ascii")):
        raise LabError("AUTHORIZATION_DENIED")


def decode_record(raw: bytes, principal_tenant: str, expected_key: str | None = None) -> bytes:
    """Validate a record and enforce the demo's tenant-scoped authorization rule."""
    if not isinstance(raw, bytes) or len(raw) > MAX_RECORD_BYTES:
        raise LabError("INVALID_RECORD")
    try:
        envelope = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, ValueError):
        raise LabError("INVALID_RECORD") from None
    fields = {"version", "tenant", "key", "payload_b64", "sha256"}
    if not isinstance(envelope, dict) or set(envelope) != fields:
        raise LabError("INVALID_RECORD")
    if type(envelope["version"]) is not int or envelope["version"] != 1:
        raise LabError("INVALID_RECORD")
    tenant = envelope["tenant"]
    key = envelope["key"]
    encoded_payload = envelope["payload_b64"]
    digest = envelope["sha256"]
    if not isinstance(tenant, str) or not isinstance(key, str) or not isinstance(encoded_payload, str):
        raise LabError("INVALID_RECORD")
    _validate_component(tenant, tenant=True)
    _validate_component(key)
    _authorize(principal_tenant, tenant)
    if expected_key is not None and key != expected_key:
        raise LabError("INVALID_RECORD")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise LabError("INVALID_RECORD")
    body = {name: envelope[name] for name in ("key", "payload_b64", "tenant", "version")}
    expected_digest = hashlib.sha256(_canonical_body(body)).hexdigest()
    if not hmac.compare_digest(digest, expected_digest):
        raise LabError("CHECKSUM_MISMATCH")
    max_encoded_payload = 4 * ((MAX_PAYLOAD_BYTES + 2) // 3)
    if len(encoded_payload) > max_encoded_payload:
        raise LabError("INPUT_TOO_LARGE")
    try:
        payload = base64.b64decode(encoded_payload, validate=True)
    except (ValueError, base64.binascii.Error):
        raise LabError("INVALID_RECORD") from None
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise LabError("INPUT_TOO_LARGE")
    return payload


def _open_directory(path: Path) -> int:
    """Open a private directory without following a symlink at its final component."""
    try:
        path_stat = path.lstat()
        if not stat.S_ISDIR(path_stat.st_mode):
            raise LabError("UNSAFE_PATH")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except LabError:
        raise
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise LabError("UNSAFE_PATH") from None
        if error.errno == errno.ENOENT:
            raise LabError("NOT_FOUND") from None
        raise LabError("IO_ERROR") from None
    details = os.fstat(fd)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != _PRIVATE_DIR_MODE
    ):
        os.close(fd)
        raise LabError("UNSAFE_PATH")
    return fd


def _open_tenant_directory(root_fd: int, tenant: str, *, create: bool) -> int:
    try:
        if create:
            try:
                os.mkdir(tenant, _PRIVATE_DIR_MODE, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
        fd = os.open(
            tenant,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES}:
            raise LabError("UNSAFE_PATH") from None
        if error.errno == errno.ENOENT:
            raise LabError("NOT_FOUND") from None
        raise LabError("IO_ERROR") from None
    details = os.fstat(fd)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != _PRIVATE_DIR_MODE
    ):
        os.close(fd)
        raise LabError("UNSAFE_PATH")
    return fd


def _validate_regular_file(fd: int) -> os.stat_result:
    details = os.fstat(fd)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != _PRIVATE_FILE_MODE
        or details.st_size > MAX_RECORD_BYTES
    ):
        raise LabError("UNSAFE_PATH")
    return details


def _write_all(fd: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise LabError("IO_ERROR")
        view = view[written:]


def write_record(root: Path, tenant: str, key: str, payload: bytes) -> None:
    """Create a new record beneath an already private, caller-owned lab root."""
    _validate_component(tenant, tenant=True)
    _validate_component(key)
    encoded = encode_record(tenant, key, payload)
    root_fd = _open_directory(Path(root))
    tenant_fd = -1
    record_fd = -1
    filename = f"{key}.record"
    created = False
    try:
        tenant_fd = _open_tenant_directory(root_fd, tenant, create=True)
        try:
            record_fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                _PRIVATE_FILE_MODE,
                dir_fd=tenant_fd,
            )
            created = True
        except FileExistsError:
            raise LabError("ALREADY_EXISTS") from None
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES}:
                raise LabError("UNSAFE_PATH") from None
            raise LabError("IO_ERROR") from None
        _validate_regular_file(record_fd)
        _write_all(record_fd, encoded)
        os.fsync(record_fd)
        os.fsync(tenant_fd)
    except OSError:
        if created and tenant_fd >= 0:
            try:
                os.unlink(filename, dir_fd=tenant_fd)
                os.fsync(tenant_fd)
            except OSError:
                pass
        raise LabError("IO_ERROR") from None
    except LabError:
        if created and tenant_fd >= 0:
            try:
                os.unlink(filename, dir_fd=tenant_fd)
                os.fsync(tenant_fd)
            except OSError:
                pass
        raise
    finally:
        if record_fd >= 0:
            os.close(record_fd)
        if tenant_fd >= 0:
            os.close(tenant_fd)
        os.close(root_fd)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(4096, MAX_RECORD_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_RECORD_BYTES:
            raise LabError("INPUT_TOO_LARGE")


def read_record(root: Path, principal_tenant: str, tenant: str, key: str) -> bytes:
    """Read a tenant-scoped record using directory-relative, no-follow opens."""
    _validate_component(principal_tenant, tenant=True)
    _validate_component(tenant, tenant=True)
    _validate_component(key)
    _authorize(principal_tenant, tenant)
    root_fd = _open_directory(Path(root))
    tenant_fd = -1
    record_fd = -1
    try:
        tenant_fd = _open_tenant_directory(root_fd, tenant, create=False)
        try:
            record_fd = os.open(
                f"{key}.record",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=tenant_fd,
            )
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES}:
                raise LabError("UNSAFE_PATH") from None
            if error.errno == errno.ENOENT:
                raise LabError("NOT_FOUND") from None
            raise LabError("IO_ERROR") from None
        details = _validate_regular_file(record_fd)
        raw = _read_all(record_fd)
        if len(raw) != details.st_size:
            raise LabError("INVALID_RECORD")
        return decode_record(raw, principal_tenant, expected_key=key)
    except OSError:
        raise LabError("IO_ERROR") from None
    finally:
        if record_fd >= 0:
            os.close(record_fd)
        if tenant_fd >= 0:
            os.close(tenant_fd)
        os.close(root_fd)


def _private_mode(path: Path, expected_mode: int) -> bool:
    details = path.lstat()
    return (
        not stat.S_ISLNK(details.st_mode)
        and details.st_uid == os.geteuid()
        and stat.S_IMODE(details.st_mode) == expected_mode
    )


def _audit_rejection(
    checks: list[dict[str, str]],
    rejections: Counter[str],
    name: str,
    expected_code: str,
    operation: Callable[[], object],
) -> None:
    try:
        operation()
    except LabError as error:
        rejections[error.code] += 1
        status = "PASS" if error.code == expected_code else "FAIL"
    except Exception:
        # Keep unexpected implementation details/paths out of the audit report.
        rejections["UNEXPECTED_ERROR"] += 1
        status = "FAIL"
    else:
        status = "FAIL"
    checks.append({"name": name, "status": status})


def run_demo() -> dict:
    """Run deterministic checks entirely inside a newly-created temporary directory."""
    checks: list[dict[str, str]] = []
    rejections: Counter[str] = Counter()
    sample = b"M14 local-only sample payload"
    with tempfile.TemporaryDirectory(prefix="storage-lab14-") as temporary:
        root = Path(temporary) / "vault"
        root.mkdir(mode=_PRIVATE_DIR_MODE)
        write_record(root, "tenant-a", "sample-1", sample)
        record_path = root / "tenant-a" / "sample-1.record"
        checks.append(
            {
                "name": "private_root_and_tenant_modes",
                "status": "PASS"
                if _private_mode(root, _PRIVATE_DIR_MODE)
                and _private_mode(root / "tenant-a", _PRIVATE_DIR_MODE)
                else "FAIL",
            }
        )
        checks.append(
            {
                "name": "private_record_mode",
                "status": "PASS" if _private_mode(record_path, _PRIVATE_FILE_MODE) else "FAIL",
            }
        )
        checks.append(
            {
                "name": "record_round_trip_and_checksum",
                "status": "PASS"
                if read_record(root, "tenant-a", "tenant-a", "sample-1") == sample
                else "FAIL",
            }
        )

        _audit_rejection(
            checks,
            rejections,
            "tenant_scope_denied",
            "AUTHORIZATION_DENIED",
            lambda: read_record(root, "tenant-b", "tenant-a", "sample-1"),
        )
        _audit_rejection(
            checks,
            rejections,
            "path_traversal_rejected",
            "INVALID_COMPONENT",
            lambda: read_record(root, "tenant-a", "../outside", "sample-1"),
        )

        tampered = json.loads(record_path.read_bytes().decode("ascii"))
        tampered["sha256"] = "0" * 64
        tampered_bytes = json.dumps(tampered).encode("ascii")
        _audit_rejection(
            checks,
            rejections,
            "corrupt_record_rejected",
            "CHECKSUM_MISMATCH",
            lambda: decode_record(tampered_bytes, "tenant-a", expected_key="sample-1"),
        )

        (root / "tenant-a" / "alias.record").symlink_to("sample-1.record")
        _audit_rejection(
            checks,
            rejections,
            "symlink_rejected_without_following",
            "UNSAFE_PATH",
            lambda: read_record(root, "tenant-a", "tenant-a", "alias"),
        )
        _audit_rejection(
            checks,
            rejections,
            "overwrite_refused",
            "ALREADY_EXISTS",
            lambda: write_record(root, "tenant-a", "sample-1", b"replacement"),
        )

    failed = [check["name"] for check in checks if check["status"] != "PASS"]
    return {
        "lab": "M14 offline storage security audit",
        "result": "FAIL" if failed else "PASS",
        "offline": True,
        "network_used": False,
        "device_paths_opened": False,
        "checks": checks,
        "error_audit": {
            "expected_rejections": dict(sorted(rejections.items())),
            "raw_input_logged": False,
            "raw_exception_text_logged": False,
        },
        "failed_checks": failed,
        "limits": [
            "The principal is a caller-supplied tenant label, not an authenticated identity or production authorization system.",
            "SHA-256 is an unkeyed corruption check, not proof of authenticity or protection against a malicious writer.",
            "Local mode bits and same-UID checks do not isolate hostile processes running as the same user.",
            "The demo creates and removes only its own temporary directory; it is not a MiniStore security certification.",
        ],
    }


def main() -> int:
    report = run_demo()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
