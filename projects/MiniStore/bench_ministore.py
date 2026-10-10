#!/usr/bin/env python3
"""Bounded black-box CLI benchmark using only self-created temporary databases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
BINARY = ROOT / "build" / "ministore"
MAX_OPERATIONS = 1000
MAX_KEYS = 1024
MAX_VALUE_BYTES = 64 * 1024
MAX_TOTAL_PAYLOAD = 32 * 1024 * 1024


def percentile(values: Iterable[int], percent: float) -> int:
    """Return the nearest-rank percentile, with rank ceil(percent * n)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    if not 0 < percent <= 100:
        raise ValueError("percent must be in (0, 100]")
    rank = max(1, math.ceil(percent * len(ordered) / 100))
    return ordered[rank - 1]


def validate_config(
    *, operations: int, key_count: int, value_size: int,
    repetitions: int, write_percent: int, seed: int,
) -> None:
    if not 1 <= operations <= MAX_OPERATIONS:
        raise ValueError(f"operations must be between 1 and {MAX_OPERATIONS}")
    if not 1 <= key_count <= MAX_KEYS:
        raise ValueError(f"key_count must be between 1 and {MAX_KEYS}")
    if not 64 <= value_size <= MAX_VALUE_BYTES:
        raise ValueError(f"value_size must be between 64 and {MAX_VALUE_BYTES}")
    if not 1 <= repetitions <= 5:
        raise ValueError("repetitions must be between 1 and 5")
    if not 0 <= write_percent <= 100:
        raise ValueError("write_percent must be between 0 and 100")
    if not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("seed must be a non-negative signed 64-bit integer")
    worst_case = (key_count + operations) * (value_size + 32)
    if worst_case > MAX_TOTAL_PAYLOAD:
        raise ValueError(
            f"worst-case generated payload exceeds {MAX_TOTAL_PAYLOAD} bytes; "
            "reduce keys, operations, or value_size"
        )


def _key(index: int) -> str:
    return f"key-{index:08d}"


def _value(seed: int, index: int, version: int, size: int) -> bytes:
    header = f"seed={seed};key={index};version={version};".encode("ascii")
    return header + b"x" * (size - len(header))


def _latency_summary(samples: list[int]) -> dict[str, int]:
    return {
        "p50_ns": percentile(samples, 50),
        "p95_ns": percentile(samples, 95),
        "p99_ns": percentile(samples, 99),
        "min_ns": min(samples),
        "max_ns": max(samples),
    }


def _run_cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        [str(BINARY), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"MiniStore command failed ({result.returncode}): {detail}")
    return result


def _run_once(
    database: Path, *, operations: int, key_count: int, value_size: int,
    write_percent: int, seed: int,
) -> dict:
    if not BINARY.is_file():
        raise RuntimeError("build/ministore is missing; run `make clean all` first")
    database.parent.mkdir(mode=0o700)
    _run_cli("init", "--dir", str(database))

    expected: list[bytes] = []
    versions = [0] * key_count
    for index in range(key_count):
        value = _value(seed, index, 0, value_size)
        _run_cli("put", "--dir", str(database), _key(index), value.decode("ascii"))
        expected.append(value)

    rng = random.Random(seed)
    trace: list[tuple[str, int]] = []
    trace_hash = hashlib.sha256()
    for _ in range(operations):
        index = rng.randrange(key_count)
        operation = "put" if rng.randrange(100) < write_percent else "get"
        trace.append((operation, index))
        trace_hash.update(operation.encode("ascii") + b"\0" + index.to_bytes(4, "big"))

    samples: list[int] = []
    write_count = 0
    read_count = 0
    started = time.perf_counter_ns()
    for operation, index in trace:
        if operation == "put":
            versions[index] += 1
            value = _value(seed, index, versions[index], value_size)
            op_started = time.perf_counter_ns()
            result = _run_cli("put", "--dir", str(database), _key(index), value.decode("ascii"))
            samples.append(time.perf_counter_ns() - op_started)
            expected[index] = value
            if not result.stdout.startswith(b"OK seq="):
                raise RuntimeError("put returned an unexpected success response")
            write_count += 1
        else:
            op_started = time.perf_counter_ns()
            result = _run_cli("get", "--dir", str(database), _key(index))
            samples.append(time.perf_counter_ns() - op_started)
            expected_line = (
                b"key\t" + _key(index).encode("ascii") + b"\tvalue_hex\t"
                + expected[index].hex().encode("ascii") + b"\n"
            )
            if result.stdout != expected_line:
                raise RuntimeError(f"get correctness mismatch for {_key(index)}")
            read_count += 1
    wall_ns = time.perf_counter_ns() - started

    digest = hashlib.sha256()
    for index, value in enumerate(expected):
        result = _run_cli("get", "--dir", str(database), _key(index))
        expected_line = (
            b"key\t" + _key(index).encode("ascii") + b"\tvalue_hex\t"
            + value.hex().encode("ascii") + b"\n"
        )
        if result.stdout != expected_line:
            raise RuntimeError(f"final dataset verification failed for {_key(index)}")
        digest.update(index.to_bytes(4, "big") + value)

    return {
        "operation_count": len(samples),
        "read_operations": read_count,
        "write_operations": write_count,
        "wall_time_ns": wall_ns,
        "throughput_ops_per_second": round(len(samples) * 1_000_000_000 / wall_ns, 3),
        "latency_ns": _latency_summary(samples),
        "trace_sha256": trace_hash.hexdigest(),
        "final_values_sha256": digest.hexdigest(),
    }


def run_benchmark(
    *, operations: int = 40, key_count: int = 8, value_size: int = 256,
    repetitions: int = 3, write_percent: int = 30, seed: int = 20261015,
) -> dict:
    """Run QD=1 CLI operations against private temporary databases, then verify all keys."""
    validate_config(
        operations=operations,
        key_count=key_count,
        value_size=value_size,
        repetitions=repetitions,
        write_percent=write_percent,
        seed=seed,
    )
    if not BINARY.is_file():
        raise RuntimeError("build/ministore is missing; run `make clean all` first")

    runs = []
    with tempfile.TemporaryDirectory(prefix="ministore-bench-") as temporary_root:
        root = Path(temporary_root)
        for repetition in range(repetitions):
            database = root / f"run-{repetition}" / "db"
            runs.append(_run_once(
                database,
                operations=operations,
                key_count=key_count,
                value_size=value_size,
                write_percent=write_percent,
                seed=seed,
            ))

    aggregate_wall_ns = sum(run["wall_time_ns"] for run in runs)
    run_latency_summary = {}
    for metric in ("p50_ns", "p95_ns", "p99_ns"):
        values = [run["latency_ns"][metric] for run in runs]
        run_latency_summary[metric] = {
            "median_across_runs": percentile(values, 50),
            "min_across_runs": min(values),
            "max_across_runs": max(values),
        }
    aggregate = {
        "runs": len(runs),
        "operations": sum(run["operation_count"] for run in runs),
        "aggregate_wall_time_ns": aggregate_wall_ns,
        "throughput_ops_per_second": round(
            sum(run["operation_count"] for run in runs) * 1_000_000_000 / aggregate_wall_ns, 3
        ),
        "per_run_latency_summary": run_latency_summary,
        "note": "Summaries across runs; request samples are not pooled into a cross-run percentile.",
    }
    binary_digest = hashlib.sha256(BINARY.read_bytes()).hexdigest()
    return {
        "benchmark": "MiniStore single-process CLI end-to-end workload",
        "config": {
            "operations_per_run": operations,
            "key_count": key_count,
            "value_size_bytes": value_size,
            "repetitions": repetitions,
            "write_percent": write_percent,
            "seed": seed,
            "trace_sha256": runs[0]["trace_sha256"],
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "kernel_release": platform.release(),
            "machine": platform.machine(),
            "ministore_binary_sha256": binary_digest,
        },
        "io_contract": {
            "request": "one CLI subprocess per get/put; latency includes process startup, open/recovery, lock, WAL fdatasync for puts, output and exit",
            "concurrency": "single process, QD=1",
            "dataset": "private TemporaryDirectory; removed automatically",
            "correctness": "every measured get and every final key checked against expected bytes",
        },
        "correctness": {
            "passed": True,
            "verified_keys_per_run": key_count,
            "final_values_sha256_by_run": [run["final_values_sha256"] for run in runs],
        },
        "runs": runs,
        "aggregate": aggregate,
        "interpretation": (
            "Local observation only. Not device IOPS, fsync durability certification, "
            "concurrent-service throughput, cloud performance, or a comparison with another product."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations", type=int, default=40)
    parser.add_argument("--keys", type=int, default=8)
    parser.add_argument("--value-size", type=int, default=256)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--write-percent", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261015)
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(
            operations=args.operations,
            key_count=args.keys,
            value_size=args.value_size,
            repetitions=args.repetitions,
            write_percent=args.write_percent,
            seed=args.seed,
        )
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
