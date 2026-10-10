#!/usr/bin/env python3
"""A loopback-only storage observability demo with synthetic metrics and traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlsplit


MAX_REQUESTS = 100
SCENARIOS = ("healthy", "latency", "capacity", "backup", "errors", "metrics-gap")
OPERATIONS = ("get", "put", "delete")
HISTOGRAM_BUCKETS_SECONDS = (0.005, 0.01, 0.02, 0.05, 0.1)
REQUEST_P95_BUDGET_MS = 10.0
CAPACITY_WARNING_RATIO = 0.90
RESTORE_TEST_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_METRIC_LINE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^{}]*)\})?\s+"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*$"
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"\\]*)"')
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def parse_prometheus(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Parse the small, deliberately strict Prometheus text subset emitted here."""
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_LINE.fullmatch(line)
        if not match:
            raise ValueError(f"invalid metric sample on line {line_number}")
        name, raw_labels, raw_value = match.groups()
        labels: dict[str, str] = {}
        if raw_labels is not None:
            for item in raw_labels.split(","):
                label_match = _LABEL.fullmatch(item)
                if not label_match:
                    raise ValueError(f"invalid metric label on line {line_number}")
                key, value = label_match.groups()
                if key in labels:
                    raise ValueError(f"duplicate metric label {key!r} on line {line_number}")
                labels[key] = value
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"non-finite metric value on line {line_number}")
        key = (name, tuple(sorted(labels.items())))
        if key in samples:
            raise ValueError(f"duplicate metric sample {name!r} on line {line_number}")
        samples[key] = value
    return samples


def _metric(
    metrics: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    labels: dict[str, str] | None = None,
) -> float | None:
    return metrics.get((name, tuple(sorted((labels or {}).items()))))


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one sample")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered) / 100))
    return ordered[rank - 1]


def _parse_log_events(text: str) -> list[dict]:
    events: list[dict] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON log on line {line_number}") from error
        if not isinstance(event, dict) or event.get("event") != "storage_request":
            raise ValueError(f"unexpected log event on line {line_number}")
        if not _TRACE_ID.fullmatch(str(event.get("trace_id", ""))):
            raise ValueError(f"invalid trace_id on log line {line_number}")
        if not _SPAN_ID.fullmatch(str(event.get("span_id", ""))):
            raise ValueError(f"invalid span_id on log line {line_number}")
        if event.get("operation") not in OPERATIONS or event.get("status") not in {"ok", "error"}:
            raise ValueError(f"invalid operation/status on log line {line_number}")
        try:
            duration_ms = float(event["duration_ms"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid duration on log line {line_number}") from error
        if not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError(f"invalid duration on log line {line_number}")
        events.append(event)
    return events


def _parse_trace_spans(text: str) -> set[tuple[str, str]]:
    spans: set[tuple[str, str]] = set()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            trace = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON trace on line {line_number}") from error
        if not isinstance(trace, dict) or not _TRACE_ID.fullmatch(str(trace.get("trace_id", ""))):
            raise ValueError(f"invalid trace record on line {line_number}")
        trace_id = trace["trace_id"]
        trace_spans = trace.get("spans")
        if not isinstance(trace_spans, list):
            raise ValueError(f"missing spans on trace line {line_number}")
        for span in trace_spans:
            if not isinstance(span, dict) or not _SPAN_ID.fullmatch(str(span.get("span_id", ""))):
                raise ValueError(f"invalid span on trace line {line_number}")
            spans.add((trace_id, span["span_id"]))
    return spans


def _histogram_p95_upper_ms(
    metrics: dict[tuple[str, tuple[tuple[str, str], ...]], float]
) -> float | None:
    buckets: list[tuple[float, float]] = []
    for (name, labels), count in metrics.items():
        if name != "storage_request_duration_seconds_bucket":
            continue
        label_map = dict(labels)
        boundary = label_map.get("le")
        if boundary is None:
            continue
        buckets.append((math.inf if boundary == "+Inf" else float(boundary), count))
    count = _metric(metrics, "storage_request_duration_seconds_count")
    if count is None or count <= 0:
        return None
    target = math.ceil(0.95 * count)
    for boundary, cumulative_count in sorted(buckets):
        if cumulative_count >= target:
            if math.isinf(boundary):
                return None
            return boundary * 1000
    return None


def analyze_observation(
    logs_text: str,
    metrics_text: str,
    traces_text: str = "",
    *,
    now: float | None = None,
) -> dict:
    """Correlate the demo's local logs, metrics and sampled trace records."""
    observed_at = time.time() if now is None else float(now)
    events = _parse_log_events(logs_text)
    metrics = parse_prometheus(metrics_text)
    trace_spans = _parse_trace_spans(traces_text)
    findings: list[dict] = []

    def finding(code: str, severity: str, message: str, evidence: dict) -> None:
        findings.append({"code": code, "severity": severity, "message": message, "evidence": evidence})

    durations = [float(event["duration_ms"]) for event in events]
    errors = sum(event["status"] == "error" for event in events)
    error_rate = errors / len(events) if events else None
    latency = None
    if durations:
        latency = {
            "sample_count": len(durations),
            "p50_ms": _nearest_rank(durations, 50),
            "p95_ms": _nearest_rank(durations, 95),
            "p99_ms": _nearest_rank(durations, 99),
            "mean_ms": sum(durations) / len(durations),
            "histogram_p95_upper_bound_ms": _histogram_p95_upper_ms(metrics),
        }
        if latency["p95_ms"] > REQUEST_P95_BUDGET_MS:
            finding(
                "P95_LATENCY_OVER_BUDGET",
                "warning",
                "Observed local request p95 exceeds the demo budget; identify the slow span before changing storage settings.",
                {"p95_ms": latency["p95_ms"], "budget_ms": REQUEST_P95_BUDGET_MS},
            )

    complete_value = _metric(metrics, "storage_metrics_snapshot_complete")
    snapshot_complete = complete_value == 1 if complete_value is not None else None
    metric_request_count: float | None = None
    request_series_count = 0
    metric_status_totals = {"ok": 0.0, "error": 0.0}
    for operation in OPERATIONS:
        for status in ("ok", "error"):
            value = _metric(metrics, "storage_requests_total", {"operation": operation, "status": status})
            if value is not None:
                request_series_count += 1
                metric_status_totals[status] += value
    if request_series_count:
        metric_request_count = sum(metric_status_totals.values())

    metric_mismatch = (
        metric_request_count is None
        or metric_request_count != len(events)
        or snapshot_complete is not True
    )
    if metric_mismatch:
        finding(
            "METRICS_INCOMPLETE",
            "warning",
            "Request metrics are absent, marked incomplete, or disagree with observed logs; do not interpret a missing series as zero traffic.",
            {
                "observed_log_events": len(events),
                "metric_request_count": metric_request_count,
                "snapshot_complete": snapshot_complete,
            },
        )

    trace_correlated = sum((event["trace_id"], event["span_id"]) in trace_spans for event in events)
    if trace_correlated < len(events):
        finding(
            "TRACE_CORRELATION_GAP",
            "warning",
            "Some observed request spans are absent from the local trace file; sampling or exporter loss can explain this and absence is not proof of no request.",
            {"observed_log_events": len(events), "trace_correlated_events": trace_correlated},
        )

    live_value = _metric(metrics, "storage_service_live")
    ready_value = _metric(metrics, "storage_service_ready")
    health = {
        "live": None if live_value is None else live_value == 1,
        "ready": None if ready_value is None else ready_value == 1,
        "state": "unknown",
    }
    if health["live"] is not None and health["ready"] is not None:
        health["state"] = "healthy" if health["live"] and health["ready"] else "degraded"
    if health["live"] is False:
        finding("PROCESS_NOT_LIVE", "critical", "The synthetic liveness signal is false.", {})
    elif health["ready"] is False:
        finding(
            "SERVICE_NOT_READY",
            "error",
            "The process is live but does not advertise readiness; inspect dependency/error evidence before restarting it.",
            {"live": health["live"], "ready": health["ready"]},
        )
    elif health["live"] is None or health["ready"] is None:
        finding("HEALTH_SIGNAL_MISSING", "warning", "Liveness/readiness metrics are unavailable, so service health is unknown.", {})

    if error_rate is not None and error_rate >= 0.10:
        finding(
            "HIGH_ERROR_RATE",
            "error",
            "The observed request error ratio is at or above the demo threshold.",
            {"errors": errors, "requests": len(events), "error_rate": error_rate},
        )

    used = _metric(metrics, "storage_capacity_bytes", {"pool": "demo", "kind": "used"})
    total = _metric(metrics, "storage_capacity_bytes", {"pool": "demo", "kind": "total"})
    capacity = None
    if used is not None and total is not None and total > 0 and 0 <= used <= total:
        utilization = used / total
        capacity = {"used_bytes": int(used), "total_bytes": int(total), "free_bytes": int(total - used), "utilization": utilization}
        if utilization >= CAPACITY_WARNING_RATIO:
            finding(
                "CAPACITY_HIGH",
                "warning",
                "Synthetic pool utilization is high; check growth rate, reserve/headroom and expansion lead time.",
                {"utilization": utilization, "warning_ratio": CAPACITY_WARNING_RATIO},
            )
    else:
        finding("CAPACITY_METRIC_UNKNOWN", "warning", "Used/total capacity metrics are missing or invalid; capacity is unknown, not zero.", {})

    last_backup = _metric(metrics, "storage_backup_last_success_timestamp_seconds")
    rpo_target = _metric(metrics, "storage_rpo_objective_seconds")
    backup = None
    if last_backup is not None and rpo_target is not None and rpo_target > 0:
        backup_age = max(0.0, observed_at - last_backup)
        backup = {"age_seconds": backup_age, "rpo_objective_seconds": rpo_target}
        if backup_age > rpo_target:
            finding(
                "RPO_OBJECTIVE_MISSED",
                "error",
                "The synthetic last-backup age exceeds the RPO objective; this is a warning about the modeled backup point, not proof of actual recoverability.",
                {"backup_age_seconds": backup_age, "rpo_objective_seconds": rpo_target},
            )
    else:
        finding("BACKUP_METRIC_UNKNOWN", "warning", "Backup timestamp/RPO metrics are missing; the recovery point is unknown.", {})

    restore_test = _metric(metrics, "storage_restore_test_timestamp_seconds")
    restore_duration = _metric(metrics, "storage_restore_test_duration_seconds")
    rto_target = _metric(metrics, "storage_rto_objective_seconds")
    restore = None
    if restore_test is not None and restore_duration is not None and rto_target is not None and rto_target > 0:
        restore_age = max(0.0, observed_at - restore_test)
        restore = {
            "last_test_age_seconds": restore_age,
            "last_test_duration_seconds": restore_duration,
            "rto_objective_seconds": rto_target,
        }
        if restore_duration > rto_target:
            finding(
                "RTO_TEST_OVER_BUDGET",
                "error",
                "The last synthetic restore rehearsal exceeded the RTO objective; a rehearsal duration is evidence, not an end-to-end SLA guarantee.",
                {"restore_duration_seconds": restore_duration, "rto_objective_seconds": rto_target},
            )
        if restore_age > RESTORE_TEST_MAX_AGE_SECONDS:
            finding(
                "RESTORE_TEST_STALE",
                "warning",
                "The last restore rehearsal is older than the demo freshness window; no recent recovery evidence is available.",
                {"last_test_age_seconds": restore_age, "freshness_window_seconds": RESTORE_TEST_MAX_AGE_SECONDS},
            )
    else:
        finding("RESTORE_TEST_UNKNOWN", "warning", "Restore rehearsal metrics are missing; recovery time and test freshness are unknown.", {})

    return {
        "schema_version": 1,
        "observed_at": observed_at,
        "health": health,
        "latency": latency,
        "capacity": capacity,
        "backup": backup,
        "restore": restore,
        "telemetry": {
            "observed_log_events": len(events),
            "metric_request_count": metric_request_count,
            "metric_request_series_present": request_series_count,
            "metrics_snapshot_complete": snapshot_complete,
            "trace_correlated_events": trace_correlated,
            "error_rate_from_observed_logs": error_rate,
        },
        "findings": findings,
        "interpretation_limits": [
            "The service and metrics are synthetic; no real storage volume, backup, restore, Kubernetes control plane or Ceph cluster is exercised.",
            "Logs, metrics and traces are separate lossy signals. Missing samples/series/spans are unknown, not proof of zero traffic or healthy storage.",
            "RPO/RTO comparisons use explicitly modeled timestamps and the last modeled rehearsal; actual recovery requires a verified restore of the intended data and dependencies.",
            "Metric labels are deliberately bounded; request IDs, trace IDs and object keys must not become metric labels.",
        ],
    }


class _DemoState:
    def __init__(self, scenario: str, seed: int, log_stream: TextIO):
        self.scenario = scenario
        self.rng = random.Random(seed)
        self.log_stream = log_stream
        self.generated_at = time.time()
        self.events: list[dict] = []
        self.total_bytes = 1_000_000_000
        self.used_bytes = 960_000_000 if scenario == "capacity" else 640_000_000
        self.backup_age_seconds = 7_200 if scenario == "backup" else 900
        self.restore_duration_seconds = 2_400 if scenario == "backup" else 480
        self.restore_age_seconds = 45 * 24 * 60 * 60 if scenario == "backup" else 3 * 24 * 60 * 60

    def record_request(
        self,
        *,
        request_number: int,
        operation: str,
        peer_ip: str,
        trace_id: str,
        parent_span_id: str,
        span_id: str,
        started_ns: int,
    ) -> tuple[int, float]:
        delay = self.rng.uniform(0.001, 0.003)
        if self.scenario == "latency" and request_number % 5 == 0:
            delay += 0.025
        time.sleep(delay)
        is_error = self.scenario == "errors" and request_number % 5 == 0
        status = "error" if is_error else "ok"
        status_code = 503 if is_error else 200
        duration_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        event = {
            "timestamp": time.time(),
            "level": "warning" if is_error else "info",
            "service": "storage-lab13",
            "event": "storage_request",
            "request_id": f"lab13-{request_number:04d}",
            "operation": operation,
            "status": status,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "bytes": 4096 if operation == "get" else 0,
            "peer_ip": peer_ip,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
        }
        self.events.append(event)
        self.log_stream.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
        self.log_stream.flush()
        return status_code, duration_ms

    def health(self) -> dict:
        error_count = sum(event["status"] == "error" for event in self.events)
        error_ratio = error_count / len(self.events) if self.events else 0.0
        live = True
        ready = error_ratio < 0.20
        capacity_high = self.used_bytes / self.total_bytes >= CAPACITY_WARNING_RATIO
        return {
            "live": live,
            "ready": ready,
            "state": "healthy" if ready and not capacity_high else "degraded",
            "observed_error_ratio": error_ratio,
        }

    def render_metrics(self) -> str:
        lines = [
            "# Synthetic Prometheus exposition; values are not device measurements.",
            f"storage_metrics_snapshot_complete {0 if self.scenario == 'metrics-gap' else 1}",
        ]
        if self.scenario != "metrics-gap":
            request_counts = Counter((event["operation"], event["status"]) for event in self.events)
            for operation in OPERATIONS:
                for status in ("ok", "error"):
                    count = request_counts[(operation, status)]
                    if count:
                        lines.append(
                            f'storage_requests_total{{operation="{operation}",status="{status}"}} {count}'
                        )
            durations = [float(event["duration_ms"]) / 1000 for event in self.events]
            for boundary in HISTOGRAM_BUCKETS_SECONDS:
                count = sum(duration <= boundary for duration in durations)
                lines.append(
                    f'storage_request_duration_seconds_bucket{{le="{boundary:g}"}} {count}'
                )
            lines.append(
                f'storage_request_duration_seconds_bucket{{le="+Inf"}} {len(durations)}'
            )
            lines.append(f"storage_request_duration_seconds_count {len(durations)}")
            lines.append(f"storage_request_duration_seconds_sum {sum(durations):.9f}")

        health = self.health()
        lines.extend(
            [
                f"storage_service_live {1 if health['live'] else 0}",
                f"storage_service_ready {1 if health['ready'] else 0}",
                f'storage_capacity_bytes{{pool="demo",kind="used"}} {self.used_bytes}',
                f'storage_capacity_bytes{{pool="demo",kind="total"}} {self.total_bytes}',
                f"storage_backup_last_success_timestamp_seconds {self.generated_at - self.backup_age_seconds:.3f}",
                "storage_rpo_objective_seconds 3600",
                f"storage_restore_test_timestamp_seconds {self.generated_at - self.restore_age_seconds:.3f}",
                f"storage_restore_test_duration_seconds {self.restore_duration_seconds}",
                "storage_rto_objective_seconds 1800",
            ]
        )
        return "\n".join(lines) + "\n"


def _make_handler(state: _DemoState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:
            # All useful application events go to the private JSONL artifact.
            return

        def _reply(self, status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                body = json.dumps(state.health(), separators=(",", ":")).encode("utf-8")
                self._reply(200, body, "application/json")
                return
            if parsed.path == "/metrics":
                self._reply(200, state.render_metrics().encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8")
                return
            operation = parsed.path.removeprefix("/")
            if operation not in OPERATIONS:
                self._reply(404, b'{"error":"not found"}', "application/json")
                return
            try:
                request_number = int(parse_qs(parsed.query).get("n", [""])[0])
            except ValueError:
                self._reply(400, b'{"error":"invalid request number"}', "application/json")
                return
            parent = _TRACEPARENT.fullmatch(self.headers.get("traceparent", ""))
            if parent is None:
                self._reply(400, b'{"error":"invalid traceparent"}', "application/json")
                return
            trace_id, parent_span_id, _flags = parent.groups()
            span_id = hashlib.sha256(f"{trace_id}:{request_number}:server".encode()).hexdigest()[:16]
            started_ns = time.perf_counter_ns()
            status, duration_ms = state.record_request(
                request_number=request_number,
                operation=operation,
                peer_ip=self.client_address[0],
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                span_id=span_id,
                started_ns=started_ns,
            )
            body = json.dumps(
                {"ok": status == 200, "operation": operation, "synthetic": True},
                separators=(",", ":"),
            ).encode("utf-8")
            self._reply(
                status,
                body,
                "application/json",
                {
                    "X-Trace-ID": trace_id,
                    "X-Server-Span-ID": span_id,
                    "X-Server-Duration-Ms": f"{duration_ms:.6f}",
                },
            )

    return Handler


def _secure_open(path: Path) -> TextIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def run_demo(
    *,
    scenario: str = "healthy",
    requests: int = 24,
    seed: int = 20261013,
    output_parent: str | os.PathLike[str] | None = None,
) -> dict:
    """Run synthetic requests against 127.0.0.1 and retain private local artifacts."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of: {', '.join(SCENARIOS)}")
    if not 1 <= requests <= MAX_REQUESTS:
        raise ValueError(f"requests must be between 1 and {MAX_REQUESTS}")
    if not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("seed must be a non-negative signed 64-bit integer")
    artifact_dir = Path(
        tempfile.mkdtemp(prefix="storage-lab13-", dir=output_parent or "/tmp")
    )
    log_path = artifact_dir / "localhost.jsonl"
    trace_path = artifact_dir / "traces.jsonl"
    metrics_path = artifact_dir / "synthetic.prom"
    health_path = artifact_dir / "health.json"
    report_path = artifact_dir / "analysis.json"

    with _secure_open(log_path) as log_stream, _secure_open(trace_path) as trace_stream:
        state = _DemoState(scenario, seed, log_stream)
        server = HTTPServer(("127.0.0.1", 0), _make_handler(state))
        thread = threading.Thread(target=server.serve_forever, name="lab13-loopback", daemon=True)
        thread.start()
        host, port = server.server_address
        loopback_client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for request_number in range(requests):
                operation = OPERATIONS[request_number % len(OPERATIONS)]
                trace_id = hashlib.sha256(f"{seed}:{request_number}:trace".encode()).hexdigest()[:32]
                client_span_id = hashlib.sha256(f"{seed}:{request_number}:client".encode()).hexdigest()[:16]
                traceparent = f"00-{trace_id}-{client_span_id}-01"
                url = f"http://{host}:{port}/{operation}?n={request_number}"
                request = urllib.request.Request(url, headers={"traceparent": traceparent}, method="GET")
                client_started_ns = time.perf_counter_ns()
                try:
                    with loopback_client.open(request, timeout=3) as response:
                        response.read()
                        status_code = response.status
                        response_headers = response.headers
                except urllib.error.HTTPError as error:
                    try:
                        error.read()
                        status_code = error.code
                        response_headers = error.headers
                    finally:
                        error.close()
                client_duration_ms = (time.perf_counter_ns() - client_started_ns) / 1_000_000
                server_span_id = response_headers.get("X-Server-Span-ID", "")
                trace_stream.write(
                    json.dumps(
                        {
                            "trace_id": trace_id,
                            "spans": [
                                {
                                    "span_id": client_span_id,
                                    "parent_span_id": None,
                                    "service": "lab13-client",
                                    "operation": operation,
                                    "duration_ms": client_duration_ms,
                                    "status_code": status_code,
                                },
                                {
                                    "span_id": server_span_id,
                                    "parent_span_id": client_span_id,
                                    "service": "storage-lab13",
                                    "operation": operation,
                                    "duration_ms": float(response_headers.get("X-Server-Duration-Ms", "0")),
                                    "status_code": status_code,
                                },
                            ],
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                )
                trace_stream.flush()

            health_request = urllib.request.Request(f"http://{host}:{port}/health", method="GET")
            with loopback_client.open(health_request, timeout=3) as response:
                health = json.loads(response.read().decode("utf-8"))
            metrics_request = urllib.request.Request(f"http://{host}:{port}/metrics", method="GET")
            with loopback_client.open(metrics_request, timeout=3) as response:
                metrics_text = response.read().decode("utf-8")
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    logs_text = log_path.read_text(encoding="utf-8")
    traces_text = trace_path.read_text(encoding="utf-8")
    with _secure_open(metrics_path) as stream:
        stream.write(metrics_text)
    with _secure_open(health_path) as stream:
        stream.write(json.dumps(health, indent=2, sort_keys=True) + "\n")

    report = analyze_observation(logs_text, metrics_text, traces_text, now=state.generated_at)
    with _secure_open(report_path) as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")

    return {
        "scenario": scenario,
        "requests_sent": requests,
        "seed": seed,
        "loopback_endpoint": f"http://{host}:{port}",
        "observed_at": state.generated_at,
        "environment": {"python": platform.python_version(), "system": platform.system(), "release": platform.release()},
        "artifact_dir": str(artifact_dir),
        "files": [log_path.name, metrics_path.name, trace_path.name, health_path.name, report_path.name],
        "analysis": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="healthy")
    parser.add_argument("--requests", type=int, default=24, help=f"synthetic requests, 1..{MAX_REQUESTS}")
    parser.add_argument("--seed", type=int, default=20261013)
    args = parser.parse_args()
    try:
        result = run_demo(scenario=args.scenario, requests=args.requests, seed=args.seed)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
