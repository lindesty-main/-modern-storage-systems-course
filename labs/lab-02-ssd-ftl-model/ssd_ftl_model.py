#!/usr/bin/env python3
"""Deterministic, in-memory teaching models for SSD FTL and block-I/O queues.

This program never opens a device or creates files. Its NAND and latency values
are deliberately synthetic and are not a hardware benchmark or SSD emulator.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import random
import stat
import sys
from pathlib import Path
from typing import Any, Iterable


MAX_PAGES = 1_000_000
MAX_PHYSICAL_BLOCKS = 10_000
MAX_MODEL_PAGES = 2_000_000
MAX_TRACE_WRITES = 1_000_000
POLICIES = {"fifo", "least_worn"}


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _number(value: Any, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return result


def validate_config(config: Any) -> None:
    """Validate the complete JSON configuration or raise ValueError."""
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    required = {"geometry", "workloads", "allocation_policies", "queue"}
    if set(config) != required:
        raise ValueError(f"config keys must be exactly: {', '.join(sorted(required))}")

    geometry = config["geometry"]
    if not isinstance(geometry, dict) or set(geometry) != {
        "page_bytes", "pages_per_block", "logical_pages", "physical_blocks"
    }:
        raise ValueError("geometry must define page_bytes, pages_per_block, logical_pages, physical_blocks")
    _integer(geometry["page_bytes"], "geometry.page_bytes", 512, 65536)
    pages_per_block = _integer(geometry["pages_per_block"], "geometry.pages_per_block", 2, 256)
    logical_pages = _integer(geometry["logical_pages"], "geometry.logical_pages", 1, MAX_PAGES)
    physical_blocks = _integer(geometry["physical_blocks"], "geometry.physical_blocks", 4, MAX_PHYSICAL_BLOCKS)
    total_pages = pages_per_block * physical_blocks
    used_blocks = (logical_pages + pages_per_block - 1) // pages_per_block
    if total_pages > MAX_MODEL_PAGES:
        raise ValueError(f"model capacity must not exceed {MAX_MODEL_PAGES} physical pages")
    if logical_pages >= total_pages or physical_blocks - used_blocks < 2:
        raise ValueError("geometry needs at least two initially erased overprovisioned blocks")

    workloads = config["workloads"]
    if not isinstance(workloads, list) or not workloads:
        raise ValueError("workloads must be a non-empty array")
    names: set[str] = set()
    for index, workload in enumerate(workloads):
        prefix = f"workloads[{index}]"
        if not isinstance(workload, dict):
            raise ValueError(f"{prefix} must be an object")
        kind = workload.get("kind")
        if not isinstance(kind, str) or kind not in {"sequential", "uniform", "hotset"}:
            raise ValueError(f"{prefix}.kind must be sequential, uniform, or hotset")
        expected = {"name", "kind", "writes", "seed"}
        if kind == "hotset":
            expected.add("hot_pages")
        if set(workload) != expected:
            raise ValueError(f"{prefix} fields do not match kind {kind!r}")
        name = workload["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError(f"{prefix}.name must be a unique non-empty string")
        names.add(name)
        _integer(workload["writes"], f"{prefix}.writes", 1, MAX_TRACE_WRITES)
        _integer(workload["seed"], f"{prefix}.seed", 0, 2**32 - 1)
        if kind == "hotset":
            _integer(workload["hot_pages"], f"{prefix}.hot_pages", 1, logical_pages)

    policies = config["allocation_policies"]
    if not isinstance(policies, list) or not policies:
        raise ValueError("allocation_policies must be a non-empty array")
    if any(not isinstance(policy, str) or policy not in POLICIES for policy in policies):
        raise ValueError("allocation_policies may contain unique fifo and/or least_worn policies")
    if len(set(policies)) != len(policies):
        raise ValueError("allocation_policies may not contain duplicates")

    queue = config["queue"]
    if queue is not None:
        _validate_queue_config(queue)


def _validate_queue_config(config: Any) -> None:
    if not isinstance(config, dict) or set(config) != {
        "requests", "interarrival_us", "request_bytes", "systems"
    }:
        raise ValueError("queue must define requests, interarrival_us, request_bytes, systems")
    _integer(config["requests"], "queue.requests", 1, MAX_TRACE_WRITES)
    _number(config["interarrival_us"], "queue.interarrival_us", 0.000001)
    _integer(config["request_bytes"], "queue.request_bytes", 0, 2**40)
    systems = config["systems"]
    if not isinstance(systems, dict) or set(systems) != {"local", "remote"}:
        raise ValueError("queue.systems must define local and remote")
    for name, system in systems.items():
        if not isinstance(system, dict) or set(system) != {
            "lanes", "fixed_us", "us_per_mib", "network_rtt_us"
        }:
            raise ValueError(f"queue.systems.{name} must define lanes, fixed_us, us_per_mib, network_rtt_us")
        _integer(system["lanes"], f"queue.systems.{name}.lanes", 1, 256)
        fixed = _number(system["fixed_us"], f"queue.systems.{name}.fixed_us")
        rate = _number(system["us_per_mib"], f"queue.systems.{name}.us_per_mib")
        _number(system["network_rtt_us"], f"queue.systems.{name}.network_rtt_us")
        if fixed == 0 and (rate == 0 or config["request_bytes"] == 0):
            raise ValueError(f"queue.systems.{name} must have positive modeled service time")


class FTLModel:
    """Page-mapped FTL toy model with greedy GC and two free-block policies."""

    def __init__(self, geometry: dict[str, int], allocation: str = "least_worn"):
        self.page_bytes = geometry["page_bytes"]
        self.pages_per_block = geometry["pages_per_block"]
        self.logical_pages = geometry["logical_pages"]
        self.physical_blocks = geometry["physical_blocks"]
        if allocation not in POLICIES:
            raise ValueError("allocation must be fifo or least_worn")
        self.allocation = allocation

        self.blocks: list[list[int | None]] = [
            [None] * self.pages_per_block for _ in range(self.physical_blocks)
        ]
        self.erased = [True] * self.physical_blocks
        self.erase_counts = [0] * self.physical_blocks
        self.mapping: list[tuple[int, int] | None] = [None] * self.logical_pages

        full_blocks, partial_pages = divmod(self.logical_pages, self.pages_per_block)
        for logical_page in range(self.logical_pages):
            block, page = divmod(logical_page, self.pages_per_block)
            self.blocks[block][page] = logical_page
            self.mapping[logical_page] = (block, page)
            self.erased[block] = False
        self.free_blocks = list(range(full_blocks + bool(partial_pages), self.physical_blocks))
        self.active_block: int | None = full_blocks if partial_pages else None
        self.write_pointer = partial_pages

        # Initial logical contents represent a pre-filled device and are excluded
        # from measured writes/programs, like a steady-state preconditioning step.
        self.host_writes = 0
        self.gc_copied_pages = 0
        self.physical_programs = 0
        self.block_erases = 0
        self.gc_runs = 0

    def _take_free_block(self) -> int:
        if not self.free_blocks:
            raise RuntimeError("no erased block available; geometry/invariant failure")
        if self.allocation == "fifo":
            block = self.free_blocks.pop(0)
        else:
            block = min(self.free_blocks, key=lambda item: (self.erase_counts[item], item))
            self.free_blocks.remove(block)
        if not self.erased[block]:
            raise RuntimeError("free-block list contains a programmed block")
        self.erased[block] = False
        self.active_block = block
        self.write_pointer = 0
        return block

    def _collect_garbage(self) -> None:
        candidates = []
        for block in range(self.physical_blocks):
            if self.erased[block]:
                continue
            if block == self.active_block and self.write_pointer != self.pages_per_block:
                continue
            invalid = self.blocks[block].count(None)
            if invalid:
                candidates.append((block, invalid))
        if not candidates:
            raise RuntimeError("no garbage-collection victim has an invalid page")

        # Greedy victim: reclaim the most invalid pages; use wear then ID as ties.
        victim, _ = min(
            candidates,
            key=lambda item: (-item[1], self.erase_counts[item[0]], item[0]),
        )
        live_pages = [page for page in self.blocks[victim] if page is not None]
        if live_pages:
            destination = self._take_free_block()
            # _take_free_block marks a fresh destination active. Move current
            # mappings in ascending page order, accounting one NAND program/page.
            for new_page, logical_page in enumerate(live_pages):
                old_location = self.mapping[logical_page]
                if old_location is None or old_location[0] != victim:
                    raise RuntimeError("FTL mapping disagrees with victim contents")
                self.blocks[destination][new_page] = logical_page
                self.mapping[logical_page] = (destination, new_page)
                self.physical_programs += 1
                self.gc_copied_pages += 1
            self.write_pointer = len(live_pages)
        else:
            # An all-invalid block can become the next write block directly.
            destination = victim
            self.active_block = victim
            self.write_pointer = 0

        self.blocks[victim] = [None] * self.pages_per_block
        self.erased[victim] = True
        self.erase_counts[victim] += 1
        self.block_erases += 1
        self.free_blocks.append(victim)
        if live_pages:
            self.active_block = destination
        else:
            self.free_blocks.remove(victim)
            self.erased[victim] = False
        self.gc_runs += 1

    def _ensure_writable_page(self) -> tuple[int, int]:
        if self.active_block is None:
            self._take_free_block()
        elif self.write_pointer == self.pages_per_block:
            # Reclaim into an erased block and continue in that block after its
            # relocated live pages. The erased victim replenishes the free pool.
            if self.free_blocks:
                self._collect_garbage()
            else:
                raise RuntimeError("active block is full and no GC reserve exists")

        assert self.active_block is not None
        if self.write_pointer >= self.pages_per_block:
            raise RuntimeError("garbage collection did not leave a writable page")
        location = (self.active_block, self.write_pointer)
        self.write_pointer += 1
        return location

    def write(self, logical_page: int) -> None:
        if isinstance(logical_page, bool) or not isinstance(logical_page, int):
            raise ValueError("logical page must be an integer")
        if not 0 <= logical_page < self.logical_pages:
            raise ValueError(f"logical page must be in 0..{self.logical_pages - 1}")

        # GC may relocate this page, so reserve/collect first and then look up
        # the current mapping before invalidating its old physical location.
        block, page = self._ensure_writable_page()
        old_location = self.mapping[logical_page]
        if old_location is None:
            raise RuntimeError("logical page has no mapping")
        old_block, old_page = old_location
        if self.blocks[old_block][old_page] != logical_page:
            raise RuntimeError("logical-to-physical mapping is inconsistent")
        self.blocks[old_block][old_page] = None

        if self.blocks[block][page] is not None:
            raise RuntimeError("attempt to program a physical page twice")
        self.blocks[block][page] = logical_page
        self.mapping[logical_page] = (block, page)
        self.host_writes += 1
        self.physical_programs += 1

    def run(self, trace: Iterable[int]) -> dict[str, Any]:
        for logical_page in trace:
            self.write(logical_page)
        self.check_invariants()
        if not self.host_writes:
            raise ValueError("trace must contain at least one write")
        minimum = min(self.erase_counts)
        maximum = max(self.erase_counts)
        return {
            "allocation_policy": self.allocation,
            "host_writes": self.host_writes,
            "logical_bytes": self.host_writes * self.page_bytes,
            "physical_programs": self.physical_programs,
            "physical_programmed_bytes": self.physical_programs * self.page_bytes,
            "gc_copied_pages": self.gc_copied_pages,
            "gc_runs": self.gc_runs,
            "write_amplification": round(self.physical_programs / self.host_writes, 6),
            "block_erases": self.block_erases,
            "erase_count_min": minimum,
            "erase_count_max": maximum,
            "erase_count_spread": maximum - minimum,
            "free_blocks": len(self.free_blocks),
        }

    def check_invariants(self) -> bool:
        seen: set[int] = set()
        for logical_page, location in enumerate(self.mapping):
            if location is None:
                raise AssertionError(f"LBA {logical_page} has no physical mapping")
            block, page = location
            if self.blocks[block][page] != logical_page or logical_page in seen:
                raise AssertionError("mapping is not one-to-one with physical page contents")
            seen.add(logical_page)
        if len(seen) != self.logical_pages:
            raise AssertionError("logical mapping lost a page")
        physical_seen: set[int] = set()
        for block, contents in enumerate(self.blocks):
            for page, logical_page in enumerate(contents):
                if logical_page is None:
                    continue
                if logical_page in physical_seen or self.mapping[logical_page] != (block, page):
                    raise AssertionError("physical page is duplicated or absent from the current mapping")
                physical_seen.add(logical_page)
        if physical_seen != seen:
            raise AssertionError("physical contents and logical map disagree")
        if len(set(self.free_blocks)) != len(self.free_blocks):
            raise AssertionError("duplicate free block")
        for block in self.free_blocks:
            if not self.erased[block] or any(page is not None for page in self.blocks[block]):
                raise AssertionError("free block is not erased")
        if self.active_block is not None:
            if self.erased[self.active_block] or self.active_block in self.free_blocks:
                raise AssertionError("active block is erased or listed as free")
            if not 0 <= self.write_pointer <= self.pages_per_block:
                raise AssertionError("active write pointer is out of range")
            if any(page is not None for page in self.blocks[self.active_block][self.write_pointer:]):
                raise AssertionError("active block has data beyond its write pointer")
        for block, contents in enumerate(self.blocks):
            if self.erased[block] and any(page is not None for page in contents):
                raise AssertionError("erased block contains a mapped page")
        return True


def make_trace(workload: dict[str, Any], logical_pages: int) -> list[int]:
    writes = workload["writes"]
    kind = workload["kind"]
    if kind == "sequential":
        return [index % logical_pages for index in range(writes)]
    rng = random.Random(workload["seed"])
    limit = workload["hot_pages"] if kind == "hotset" else logical_pages
    return [rng.randrange(limit) for _ in range(writes)]


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def simulate_queue(config: dict[str, Any]) -> dict[str, Any]:
    """FCFS queue model; server service lanes and network RTT are explicit."""
    _validate_queue_config(config)
    results: dict[str, Any] = {}
    for name, system in config["systems"].items():
        lanes = system["lanes"]
        service_us = system["fixed_us"] + (
            config["request_bytes"] / (1024 * 1024) * system["us_per_mib"]
        )
        available = [(0.0, lane) for lane in range(lanes)]
        heapq.heapify(available)
        responses: list[float] = []
        waits: list[float] = []
        for index in range(config["requests"]):
            arrival = index * config["interarrival_us"]
            free_at, lane = heapq.heappop(available)
            start = max(arrival, free_at)
            wait = start - arrival
            finish = start + service_us
            waits.append(wait)
            responses.append(wait + service_us + system["network_rtt_us"])
            heapq.heappush(available, (finish, lane))
        results[name] = {
            "requests": config["requests"],
            "lanes": lanes,
            "modeled_service_us": round(service_us, 3),
            "network_rtt_us": system["network_rtt_us"],
            "mean_response_us": round(sum(responses) / len(responses), 3),
            "p50_us": round(_percentile(responses, 0.50), 3),
            "p95_us": round(_percentile(responses, 0.95), 3),
            "p99_us": round(_percentile(responses, 0.99), 3),
            "max_queue_wait_us": round(max(waits), 3),
        }
    return results


def run_config(config: dict[str, Any]) -> dict[str, Any]:
    validate_config(config)
    logical_pages = config["geometry"]["logical_pages"]
    ftl_results = []
    for workload in config["workloads"]:
        trace = make_trace(workload, logical_pages)
        for policy in config["allocation_policies"]:
            model = FTLModel(config["geometry"], policy)
            summary = model.run(trace)
            ftl_results.append({"workload": workload["name"], **summary})
    queue_results = simulate_queue(config["queue"]) if config["queue"] is not None else None
    return {
        "model_scope": "synthetic page-mapped FTL and FCFS queue; not hardware emulation or a benchmark",
        "initial_mapping_programs_excluded": True,
        "ftl": ftl_results,
        "queue": queue_results,
    }


def _load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant is not allowed: {value}")

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("config path must be a regular file (devices, directories, and symlinks are rejected)")
    with path.open("r", encoding="utf-8") as source:
        opened = source.fileno()
        after = os.fstat(opened)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("config file changed during open; refusing to read it")
        return json.load(source, parse_constant=reject_constant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="read-only JSON model configuration")
    args = parser.parse_args(argv)
    try:
        result = run_config(_load_json(args.config))
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
