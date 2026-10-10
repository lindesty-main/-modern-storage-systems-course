#!/usr/bin/env python3
"""Read-only environment inventory and M00 storage requirement-card validator."""

from __future__ import annotations

import argparse
import json
import math
import platform
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

MAX_CARD_BYTES = 1024 * 1024
ACCESS_PATTERNS = {"sequential", "random", "mixed"}


def _read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    """Read the standard OS identity file without executing shell content."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        try:
            parsed = shlex.split(raw_value, comments=False, posix=True)
        except ValueError:
            continue
        if parsed:
            values[key] = parsed[0]
    return values


def check_environment() -> dict[str, Any]:
    """Return a host inventory; do not write files, elevate privileges, or use network."""
    release = _read_os_release()
    architecture = platform.machine()
    os_id = release.get("ID", "unknown").lower()
    os_version = release.get("VERSION_ID", "unknown")
    ubuntu_series_match = os_id == "ubuntu" and os_version.startswith("24.04")
    tools = ("gcc", "make", "cmake", "git", "fio")
    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "os": {
            "pretty_name": release.get("PRETTY_NAME", platform.system()),
            "id": os_id,
            "version_id": os_version,
        },
        "architecture": architecture,
        "kernel": {
            "release": platform.release(),
            "candidate_matrix_note": (
                "Candidate baseline: Ubuntu 24.04.5 amd64 with GA Linux 6.8.0-146.146. "
                "uname does not identify the full package revision; verify installed packages separately."
            ),
        },
        "candidate_baseline": {
            "ubuntu_24_04_series": "match" if ubuntu_series_match else "different; not a failure",
            "amd64": "match" if architecture in {"x86_64", "amd64"} else "different; not a failure",
            "ga_kernel_package": "not inferred from uname; record package inventory if needed",
        },
        "tools": {name: shutil.which(name) is not None for name in tools},
        "effects": "read-only inventory only: no files, mounts, devices, network, installs, or privilege changes",
    }


def _object(parent: dict[str, Any], key: str, label: str, errors: list[str]) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return {}
    return value


def _nonempty_string(value: Any, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")


def _number(
    value: Any,
    label: str,
    errors: list[str],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
    exclusive_minimum: bool = False,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} must be a number" + (" (integer)" if integer else ""))
        return
    if integer and not isinstance(value, int):
        errors.append(f"{label} must be an integer")
        return
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f"{label} must be finite")
        return
    if minimum is not None and (value < minimum or (exclusive_minimum and value == minimum)):
        errors.append(f"{label} must be at least {minimum}" + (" (exclusive)" if exclusive_minimum else ""))
    if maximum is not None and value > maximum:
        errors.append(f"{label} must be at most {maximum}")


def _string_list(value: Any, label: str, errors: list[str], *, allow_empty: bool = False) -> None:
    if not isinstance(value, list):
        errors.append(f"{label} must be a list of non-empty strings")
        return
    if not allow_empty and not value:
        errors.append(f"{label} must contain at least one item")
    for index, item in enumerate(value):
        _nonempty_string(item, f"{label}[{index}]", errors)


def validate_card(card: Any) -> list[str]:
    """Validate the documented schema and cross-field invariants; return all errors."""
    errors: list[str] = []
    if not isinstance(card, dict):
        return ["card must be a JSON object"]

    schema_version = card.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        errors.append("schema_version must be the integer 1")
    _nonempty_string(card.get("service"), "service", errors)

    data = _object(card, "data", "data", errors)
    _number(data.get("initial_gib"), "data.initial_gib", errors, minimum=0, exclusive_minimum=True)
    _number(data.get("growth_gib_per_day"), "data.growth_gib_per_day", errors, minimum=0)
    _number(data.get("retention_days"), "data.retention_days", errors, minimum=0, integer=True, exclusive_minimum=True)
    _nonempty_string(data.get("sensitivity"), "data.sensitivity", errors)

    workload = _object(card, "workload", "workload", errors)
    _number(workload.get("read_percent"), "workload.read_percent", errors, minimum=0, maximum=100, integer=True)
    _number(workload.get("write_percent"), "workload.write_percent", errors, minimum=0, maximum=100, integer=True)
    read_percent = workload.get("read_percent")
    write_percent = workload.get("write_percent")
    if (
        isinstance(read_percent, int)
        and not isinstance(read_percent, bool)
        and isinstance(write_percent, int)
        and not isinstance(write_percent, bool)
        and read_percent + write_percent != 100
    ):
        errors.append("workload.read_percent and write_percent must sum to 100")
    for key in ("request_bytes", "working_set_gib", "peak_ops_per_second", "concurrency", "burst_seconds"):
        _number(
            workload.get(key),
            f"workload.{key}",
            errors,
            minimum=0,
            integer=key != "working_set_gib",
            exclusive_minimum=True,
        )
    pattern = workload.get("access_pattern")
    if not isinstance(pattern, str) or pattern not in ACCESS_PATTERNS:
        errors.append("workload.access_pattern must be sequential, random, or mixed")

    slo = _object(card, "slo", "slo", errors)
    _number(slo.get("p99_latency_ms"), "slo.p99_latency_ms", errors, minimum=0, exclusive_minimum=True)
    _number(slo.get("throughput_ops_per_second"), "slo.throughput_ops_per_second", errors, minimum=0, integer=True, exclusive_minimum=True)
    _number(slo.get("availability_percent"), "slo.availability_percent", errors, minimum=0, maximum=100)
    _number(slo.get("measurement_window_seconds"), "slo.measurement_window_seconds", errors, minimum=0, integer=True, exclusive_minimum=True)

    semantics = _object(card, "semantics", "semantics", errors)
    _nonempty_string(semantics.get("consistency"), "semantics.consistency", errors)
    _nonempty_string(semantics.get("durability"), "semantics.durability", errors)
    _number(semantics.get("rpo_seconds"), "semantics.rpo_seconds", errors, minimum=0, integer=True)
    _number(semantics.get("rto_seconds"), "semantics.rto_seconds", errors, minimum=0, integer=True)

    failure = _object(card, "failure_model", "failure_model", errors)
    for key in ("in_scope", "out_of_scope", "assumptions"):
        _string_list(failure.get(key), f"failure_model.{key}", errors)

    constraints = _object(card, "constraints", "constraints", errors)
    for key in ("deployment_boundary", "budget_and_cost", "operational_owner"):
        _nonempty_string(constraints.get(key), f"constraints.{key}", errors)
    _string_list(constraints.get("security_and_compliance"), "constraints.security_and_compliance", errors)

    options = card.get("candidate_options")
    if not isinstance(options, list):
        errors.append("candidate_options must be a list with at least two options")
    else:
        if len(options) < 2:
            errors.append("candidate_options must contain at least two options for comparison")
        for index, option in enumerate(options):
            label = f"candidate_options[{index}]"
            if not isinstance(option, dict):
                errors.append(f"{label} must be an object")
                continue
            for key in ("name", "fit", "risk"):
                _nonempty_string(option.get(key), f"{label}.{key}", errors)

    _string_list(card.get("open_questions"), "open_questions", errors, allow_empty=True)
    return errors


def _validate_file(path_text: str) -> int:
    path = Path(path_text)
    if path.is_symlink():
        print("refusing a symbolic-link card path", file=sys.stderr)
        return 2
    try:
        if not path.is_file():
            print("card path must be an existing regular file", file=sys.stderr)
            return 2
        if path.stat().st_size > MAX_CARD_BYTES:
            print(f"card exceeds the {MAX_CARD_BYTES}-byte safety limit", file=sys.stderr)
            return 2
        with path.open("r", encoding="utf-8") as stream:
            card = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        print(f"cannot read JSON card: {error}", file=sys.stderr)
        return 2

    errors = validate_card(card)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"VALID: {json.dumps(card['service'], ensure_ascii=False)}")
    print("Schema and consistency checks passed; feasibility, cost, performance, and SLO attainment are not evaluated.")
    return 0


def _print_check(as_json: bool) -> int:
    report = check_environment()
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print("M00 read-only local environment inventory")
    print(f"Python: {report['python']['implementation']} {report['python']['version']}")
    print(f"OS: {report['os']['pretty_name']} ({report['os']['id']} {report['os']['version_id']})")
    print(f"Architecture: {report['architecture']}")
    print(f"Kernel release: {report['kernel']['release']}")
    available_tools = ", ".join(name for name, found in report["tools"].items() if found)
    print("Available tools: " + (available_tools or "none of gcc/make/cmake/git/fio"))
    print("Candidate comparison: " + json.dumps(report["candidate_baseline"], ensure_ascii=False, sort_keys=True))
    print(report["effects"])
    print("Inventory is not an integration or compatibility test.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="print a read-only host inventory")
    check_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    validate_parser = subparsers.add_parser("validate", help="validate a requirement card JSON file")
    validate_parser.add_argument("card", help="existing regular JSON file; it is only read")
    args = parser.parse_args(argv)
    if args.command == "check":
        return _print_check(args.json)
    return _validate_file(args.card)


if __name__ == "__main__":
    raise SystemExit(main())
