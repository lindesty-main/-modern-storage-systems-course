#!/usr/bin/env python3
"""Safe local-file benchmark and deterministic in-memory cache-policy lab."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import struct
import tempfile
import time
from collections import OrderedDict
from typing import Iterable


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_OPERATIONS = 20_000
MAX_REPETITIONS = 10


def percentile(values: Iterable[int], percent: float) -> int:
    """Return the nearest-rank percentile (ceil(percent * n / 100))."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    if not 0 < percent <= 100:
        raise ValueError("percent must be in (0, 100]")
    rank = max(1, math.ceil(percent * len(ordered) / 100))
    return ordered[rank - 1]


def _validate_file_config(
    mode: str,
    blocks: int,
    block_size: int,
    operations: int,
    repetitions: int,
    seed: int,
    write_percent: int,
) -> None:
    if mode not in {"seqread", "randread", "mixed"}:
        raise ValueError("mode must be seqread, randread, or mixed")
    if blocks < 1 or block_size < 512:
        raise ValueError("blocks must be positive and block_size must be at least 512")
    if blocks * block_size > MAX_FILE_BYTES:
        raise ValueError(f"generated file is limited to {MAX_FILE_BYTES} bytes")
    if not 1 <= operations <= MAX_OPERATIONS:
        raise ValueError(f"operations must be between 1 and {MAX_OPERATIONS}")
    if not 1 <= repetitions <= MAX_REPETITIONS:
        raise ValueError(f"repetitions must be between 1 and {MAX_REPETITIONS}")
    if not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("seed must be a non-negative signed 64-bit integer")
    if not 0 <= write_percent <= 100:
        raise ValueError("write_percent must be between 0 and 100")


def _pattern(seed: int, purpose: str, index: int, length: int) -> bytes:
    prefix = purpose.encode("ascii") + struct.pack(">qQ", seed, index)
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hashlib.sha256(prefix + struct.pack(">Q", counter)).digest())
        counter += 1
    return bytes(output[:length])


def _pread_exact(fd: int, length: int, offset: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        part = os.pread(fd, length - len(chunks), offset + len(chunks))
        if not part:
            raise RuntimeError(f"short read at offset {offset + len(chunks)}")
        chunks.extend(part)
    return bytes(chunks)


def _pwrite_exact(fd: int, data: bytes, offset: int) -> None:
    written = 0
    while written < len(data):
        count = os.pwrite(fd, data[written:], offset + written)
        if count <= 0:
            raise RuntimeError(f"short write at offset {offset + written}")
        written += count


def _latency_summary(samples_ns: list[int]) -> dict[str, int]:
    return {
        "p50": percentile(samples_ns, 50),
        "p95": percentile(samples_ns, 95),
        "p99": percentile(samples_ns, 99),
        "min": min(samples_ns),
        "max": max(samples_ns),
    }


def _trace_sha256(trace: Iterable[tuple[str, int]]) -> str:
    digest = hashlib.sha256()
    for kind, index in trace:
        digest.update(b"W" if kind == "write" else b"R")
        digest.update(index.to_bytes(8, "big", signed=False))
    return digest.hexdigest()


def _make_trace(
    mode: str,
    blocks: int,
    operations: int,
    seed: int,
    write_percent: int,
) -> list[tuple[str, int]]:
    rng = random.Random(seed)
    trace: list[tuple[str, int]] = []
    for operation in range(operations):
        index = operation % blocks if mode == "seqread" else rng.randrange(blocks)
        kind = "read"
        if mode == "mixed" and rng.randrange(100) < write_percent:
            kind = "write"
        trace.append((kind, index))
    return trace


def run_file_benchmark(
    *,
    mode: str = "randread",
    blocks: int = 1024,
    block_size: int = 4096,
    operations: int = 1000,
    repetitions: int = 3,
    seed: int = 20261009,
    write_percent: int = 10,
) -> dict:
    """Benchmark buffered pread/pwrite on an internally-created regular file."""
    _validate_file_config(
        mode, blocks, block_size, operations, repetitions, seed, write_percent
    )
    trace = _make_trace(mode, blocks, operations, seed, write_percent)
    trace_sha256 = _trace_sha256(trace)
    initial = [_pattern(seed, "initial", index, block_size) for index in range(blocks)]
    all_samples: list[int] = []
    total_elapsed_ns = 0
    runs = []
    total_read_bytes = 0
    total_written_bytes = 0

    # No user-supplied file path is accepted: all benchmark data lives in this
    # private temporary directory and is removed automatically on exit.
    with tempfile.TemporaryDirectory(prefix="storage-lab12-") as private_dir:
        file_path = os.path.join(private_dir, "synthetic-data.bin")
        for repetition in range(repetitions):
            fd = os.open(file_path, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
            try:
                os.ftruncate(fd, blocks * block_size)
                for index, data in enumerate(initial):
                    _pwrite_exact(fd, data, index * block_size)
                os.fsync(fd)  # Dataset creation is outside the measured interval.
                expected = list(initial)
                samples_ns: list[int] = []
                read_bytes = 0
                written_bytes = 0

                started = time.perf_counter_ns()
                for operation, (kind, index) in enumerate(trace):
                    offset = index * block_size
                    if kind == "read":
                        expected_block = expected[index]
                        op_started = time.perf_counter_ns()
                        actual_block = _pread_exact(fd, block_size, offset)
                        duration = time.perf_counter_ns() - op_started
                        if actual_block != expected_block:
                            raise RuntimeError(
                                f"read verification failed at block {index}, "
                                f"operation {operation}"
                            )
                        read_bytes += block_size
                    else:
                        data = _pattern(seed, "write", operation * blocks + index, block_size)
                        op_started = time.perf_counter_ns()
                        _pwrite_exact(fd, data, offset)
                        duration = time.perf_counter_ns() - op_started
                        expected[index] = data
                        written_bytes += block_size
                    samples_ns.append(duration)
                elapsed_ns = time.perf_counter_ns() - started

                actual_digest = hashlib.sha256()
                expected_digest = hashlib.sha256()
                for index, expected_block in enumerate(expected):
                    actual_block = _pread_exact(fd, block_size, index * block_size)
                    if actual_block != expected_block:
                        raise RuntimeError(f"final data verification failed at block {index}")
                    actual_digest.update(actual_block)
                    expected_digest.update(expected_block)
                if actual_digest.digest() != expected_digest.digest():
                    raise RuntimeError("final dataset checksum mismatch")

                transferred = read_bytes + written_bytes
                elapsed_seconds = elapsed_ns / 1_000_000_000
                runs.append(
                    {
                        "repetition": repetition + 1,
                        "operations": operations,
                        "read_bytes": read_bytes,
                        "written_bytes": written_bytes,
                        "elapsed_seconds": elapsed_seconds,
                        "operations_per_second": operations / elapsed_seconds,
                        "mib_per_second": transferred / (1024 * 1024) / elapsed_seconds,
                        "latency_ns": _latency_summary(samples_ns),
                        "final_sha256": actual_digest.hexdigest(),
                    }
                )
                all_samples.extend(samples_ns)
                total_elapsed_ns += elapsed_ns
                total_read_bytes += read_bytes
                total_written_bytes += written_bytes
            finally:
                os.close(fd)

    total_ops = operations * repetitions
    total_seconds = total_elapsed_ns / 1_000_000_000
    total_bytes = total_read_bytes + total_written_bytes
    return {
        "benchmark": "buffered_local_regular_file",
        "config": {
            "mode": mode,
            "blocks": blocks,
            "block_size_bytes": block_size,
            "dataset_bytes": blocks * block_size,
            "operations_per_repetition": operations,
            "repetitions": repetitions,
            "seed": seed,
            "write_percent": write_percent if mode == "mixed" else 0,
            "queue_depth": 1,
            "workers": 1,
        },
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "not-reported",
            "logical_cpu_count": os.cpu_count(),
        },
        "workload_trace_sha256": trace_sha256,
        "io_semantics": {
            "file_type": "ordinary regular file in private temporary directory",
            "api": "buffered POSIX pread/pwrite; no O_DIRECT",
            "write_durability": "initial setup fsync only; individual writes are not synced",
            "cache_state": "not controlled; later repetitions may reuse page cache",
            "device_or_cloud_access": False,
        },
        "correctness": {
            "passed": True,
            "read_value_checks": sum(
                1 for kind, _ in trace if kind == "read"
            ) * repetitions,
            "final_dataset_checks": blocks * repetitions,
            "final_sha256_by_repetition": [run["final_sha256"] for run in runs],
        },
        "runs": runs,
        "aggregate": {
            "operations": total_ops,
            "elapsed_seconds": total_seconds,
            "operations_per_second": total_ops / total_seconds,
            "mib_per_second": total_bytes / (1024 * 1024) / total_seconds,
            "latency_ns": _latency_summary(all_samples),
        },
        "interpretation": (
            "Timing observations for this Python, buffered, single-worker, QD=1 "
            "synthetic workload only; not a device or product performance claim."
        ),
    }


def _cache_value(key: int) -> bytes:
    encoded = key.to_bytes(8, "big", signed=False)
    return b"ministore-synthetic-value:" + encoded + hashlib.sha256(encoded).digest()


def simulate_cache_trace(trace: Iterable[int], policy: str, capacity: int) -> dict:
    """Verify a bounded in-memory read-through cache policy against a backend."""
    if policy not in {"none", "lru", "fifo"}:
        raise ValueError("policy must be none, lru, or fifo")
    if capacity < 0:
        raise ValueError("capacity must be non-negative")
    keys = list(trace)
    if any(not isinstance(key, int) or key < 0 for key in keys):
        raise ValueError("trace keys must be non-negative integers")
    backend = {key: _cache_value(key) for key in set(keys)}
    cache: OrderedDict[int, bytes] = OrderedDict()
    digest = hashlib.sha256()
    hits = misses = evictions = 0

    for key in keys:
        expected = backend[key]
        if policy != "none" and key in cache:
            value = cache[key]
            hits += 1
            if policy == "lru":
                cache.move_to_end(key)
        else:
            value = backend[key]
            misses += 1
            if policy != "none" and capacity > 0:
                if len(cache) >= capacity:
                    cache.popitem(last=False)
                    evictions += 1
                cache[key] = value
        if value != expected:
            raise RuntimeError(f"cache returned incorrect value for key {key}")
        digest.update(key.to_bytes(8, "big", signed=False))
        digest.update(value)

    return {
        "policy": policy,
        "capacity_entries": capacity,
        "operations": len(keys),
        "hits": hits,
        "misses": misses,
        "evictions": evictions,
        "resident_entries": len(cache),
        "hit_rate": hits / len(keys) if keys else 0.0,
        "result_sha256": digest.hexdigest(),
    }


def run_cache_simulation(
    *, keys: int = 1024, operations: int = 12000, capacity: int = 32, seed: int = 20261009
) -> dict:
    if keys < 1 or operations < 1:
        raise ValueError("keys and operations must be positive")
    if keys > (1 << 64):
        raise ValueError("keys must fit in an unsigned 64-bit key space")
    if capacity < 0:
        raise ValueError("capacity must be non-negative")
    if operations > MAX_OPERATIONS:
        raise ValueError(f"operations must not exceed {MAX_OPERATIONS}")
    if not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("seed must be a non-negative signed 64-bit integer")

    rng = random.Random(seed)
    hot_keys = max(1, keys // 10)
    trace = [
        rng.randrange(hot_keys) if rng.randrange(100) < 80 else rng.randrange(keys)
        for _ in range(operations)
    ]
    trace_digest = hashlib.sha256()
    for key in trace:
        trace_digest.update(key.to_bytes(8, "big", signed=False))

    policies = {
        policy: simulate_cache_trace(trace, policy, capacity)
        for policy in ("none", "lru", "fifo")
    }
    result_hashes = {policy: result["result_sha256"] for policy, result in policies.items()}
    return {
        "benchmark": "ministore_like_cache_policy_simulation",
        "config": {
            "keys": keys,
            "operations": operations,
            "capacity_entries": capacity,
            "seed": seed,
            "hot_key_count": hot_keys,
            "hot_access_probability_percent": 80,
        },
        "trace_sha256": trace_digest.hexdigest(),
        "policies": policies,
        "result_sha256": result_hashes,
        "correctness": {
            "passed": len(set(result_hashes.values())) == 1,
            "same_trace_and_values_across_policies": len(set(result_hashes.values())) == 1,
        },
        "performance_observation": False,
        "interpretation": (
            "Hit/miss simulation only. This does not benchmark MiniStore: MiniStore "
            "currently loads its complete key/value state into an in-memory map and "
            "has no configurable block cache."
        ),
    }


def _add_common_seed(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=20261009)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    file_parser = commands.add_parser("file", help="benchmark an internal temporary regular file")
    file_parser.add_argument("--mode", choices=("seqread", "randread", "mixed"), default="randread")
    file_parser.add_argument("--blocks", type=int, default=1024)
    file_parser.add_argument("--block-size", type=int, default=4096)
    file_parser.add_argument("--operations", type=int, default=1000)
    file_parser.add_argument("--repetitions", type=int, default=3)
    file_parser.add_argument("--write-percent", type=int, default=10)
    _add_common_seed(file_parser)

    cache_parser = commands.add_parser("cache", help="simulate MiniStore-like LRU/FIFO cache policies")
    cache_parser.add_argument("--keys", type=int, default=1024)
    cache_parser.add_argument("--operations", type=int, default=12000)
    cache_parser.add_argument("--capacity", type=int, default=32)
    _add_common_seed(cache_parser)

    args = parser.parse_args()
    try:
        if args.command == "file":
            report = run_file_benchmark(
                mode=args.mode,
                blocks=args.blocks,
                block_size=args.block_size,
                operations=args.operations,
                repetitions=args.repetitions,
                seed=args.seed,
                write_percent=args.write_percent,
            )
        else:
            report = run_cache_simulation(
                keys=args.keys,
                operations=args.operations,
                capacity=args.capacity,
                seed=args.seed,
            )
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
