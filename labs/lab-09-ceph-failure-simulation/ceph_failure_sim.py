#!/usr/bin/env python3
"""Toy model of replica placement, failure-domain loss, and rebuild pressure.

This is a deterministic teaching simulation, not a Ceph implementation or
performance benchmark. Each named failure domain is modeled as one OSD.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Sequence


def _validate_domains(domains: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(domain.strip() for domain in domains)
    if not normalized or any(not domain for domain in normalized):
        raise ValueError("at least one non-empty failure domain is required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("failure-domain names must be unique")
    return normalized


def _score(seed: int, object_id: str, domain: str) -> bytes:
    material = f"{seed}\0{object_id}\0{domain}".encode("utf-8")
    return hashlib.sha256(material).digest()


def place_replicas(
    object_id: str,
    domains: Sequence[str],
    replicas: int,
    seed: int = 1,
) -> tuple[str, ...]:
    """Return deterministic replica domains using a hash-ranked toy placement."""
    normalized = _validate_domains(domains)
    if not object_id:
        raise ValueError("object_id must not be empty")
    if replicas < 1 or replicas > len(normalized):
        raise ValueError("replicas must be between 1 and the number of domains")
    ranked = sorted(normalized, key=lambda d: (_score(seed, object_id, d), d))
    return tuple(ranked[:replicas])


@dataclass(frozen=True)
class SimulationResult:
    objects: int
    domains: tuple[str, ...]
    replicas: int
    min_size_proxy: int
    failed_domains: tuple[str, ...]
    readable_objects: int
    unreadable_objects: int
    min_size_objects: int
    degraded_objects: int
    missing_replica_copies: int
    rebuildable_copies: int
    unrebuildable_copies: int
    requested_rebuild_iops: float
    effective_rebuild_iops: float
    nominal_rebuild_seconds: float | None
    foreground_iops: float
    capacity_iops: float
    combined_iops: float
    combined_utilization_pct: float
    remaining_headroom_iops: float
    overload_iops: float


def simulate(
    *,
    objects: int,
    domains: Sequence[str],
    replicas: int,
    failed_domains: Sequence[str] = (),
    min_size: int | None = None,
    foreground_iops: float = 70,
    rebuild_iops: float = 20,
    capacity_iops: float = 100,
    seed: int = 1,
) -> SimulationResult:
    """Simulate one or more domain failures and a fixed-rate rebuild workload.

    The IOPS values are abstract work units per second. A rebuild work unit is
    one replica-copy operation, not bytes or a measured device I/O.
    """
    normalized = _validate_domains(domains)
    failed = tuple(failed_domains)
    if len(set(failed)) != len(failed):
        raise ValueError("failed-domain names must be unique")
    unknown = set(failed) - set(normalized)
    if unknown:
        raise ValueError(f"unknown failed domains: {', '.join(sorted(unknown))}")
    if objects < 1:
        raise ValueError("objects must be at least 1")
    if replicas < 1 or replicas > len(normalized):
        raise ValueError("replicas must be between 1 and the number of domains")
    if min_size is None:
        min_size = min(replicas, 2)
    if min_size < 1 or min_size > replicas:
        raise ValueError("min_size must be between 1 and replicas")
    if foreground_iops < 0 or rebuild_iops < 0:
        raise ValueError("foreground_iops and rebuild_iops must be non-negative")
    if capacity_iops <= 0:
        raise ValueError("capacity_iops must be greater than zero")

    readable = 0
    unreadable = 0
    min_size_count = 0
    degraded = 0
    missing_copies = 0
    rebuildable = 0
    unrebuildable = 0
    rebuilt_copies_per_domain = {domain: 0 for domain in normalized}

    for object_number in range(objects):
        object_id = f"object-{object_number:08d}"
        original = place_replicas(object_id, normalized, replicas, seed)
        survivors = [domain for domain in original if domain not in failed]
        missing = replicas - len(survivors)
        if missing:
            degraded += 1
            missing_copies += missing

        if survivors:
            readable += 1
        else:
            unreadable += 1
            unrebuildable += missing
            continue
        if len(survivors) >= min_size:
            min_size_count += 1
        # Rebuild only onto live domains that do not already hold this object's
        # surviving copy. Assign to the least-used target domain for rough balance.
        candidates = [
            domain
            for domain in normalized
            if domain not in failed and domain not in survivors
        ]
        for copy_number in range(missing):
            if not candidates:
                unrebuildable += 1
                continue
            target = min(
                candidates,
                key=lambda domain: (
                    rebuilt_copies_per_domain[domain],
                    _score(seed + copy_number + 1, object_id, domain),
                    domain,
                ),
            )
            candidates.remove(target)
            rebuilt_copies_per_domain[target] += 1
            rebuildable += 1

    effective_rebuild_iops = rebuild_iops if rebuildable else 0.0
    nominal_seconds: float | None
    if rebuildable == 0:
        nominal_seconds = 0.0
    elif effective_rebuild_iops == 0:
        nominal_seconds = None
    else:
        nominal_seconds = rebuildable / effective_rebuild_iops

    combined_iops = foreground_iops + effective_rebuild_iops
    headroom = capacity_iops - combined_iops
    return SimulationResult(
        objects=objects,
        domains=normalized,
        replicas=replicas,
        min_size_proxy=min_size,
        failed_domains=failed,
        readable_objects=readable,
        unreadable_objects=unreadable,
        min_size_objects=min_size_count,
        degraded_objects=degraded,
        missing_replica_copies=missing_copies,
        rebuildable_copies=rebuildable,
        unrebuildable_copies=unrebuildable,
        requested_rebuild_iops=rebuild_iops,
        effective_rebuild_iops=effective_rebuild_iops,
        nominal_rebuild_seconds=nominal_seconds,
        foreground_iops=foreground_iops,
        capacity_iops=capacity_iops,
        combined_iops=combined_iops,
        combined_utilization_pct=round(combined_iops / capacity_iops * 100, 2),
        remaining_headroom_iops=round(headroom, 2),
        overload_iops=round(max(0.0, -headroom), 2),
    )


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely simulate replicated data across failure domains."
    )
    parser.add_argument("--objects", type=int, default=120)
    parser.add_argument("--domains", default="rack-a,rack-b,rack-c,rack-d")
    parser.add_argument("--replicas", type=int, default=3)
    parser.add_argument(
        "--failed-domain", action="append", default=[],
        help="domain to fail; may be supplied multiple times",
    )
    parser.add_argument("--min-size", type=int, default=None)
    parser.add_argument("--foreground-iops", type=float, default=75)
    parser.add_argument("--rebuild-iops", type=float, default=20)
    parser.add_argument("--capacity-iops", type=float, default=100)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    try:
        result = simulate(
            objects=args.objects,
            domains=_csv(args.domains),
            replicas=args.replicas,
            failed_domains=args.failed_domain,
            min_size=args.min_size,
            foreground_iops=args.foreground_iops,
            rebuild_iops=args.rebuild_iops,
            capacity_iops=args.capacity_iops,
            seed=args.seed,
        )
    except ValueError as error:
        parser.error(str(error))

    output = asdict(result)
    output["scope_notes"] = [
        "Toy hash-ranked placement, not the Ceph CRUSH algorithm.",
        "Each failure domain is one equal-capacity OSD; no PG, peering, EC, or network model.",
        "Readable/min_size counts are simplified proxies, not Ceph availability guarantees.",
        "IOPS are abstract work units; utilization/overload is not a latency or p99 prediction.",
        "No disks, cluster services, or real data are accessed or modified.",
    ]
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
