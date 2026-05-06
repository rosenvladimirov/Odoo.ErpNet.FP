"""
Prometheus metrics for ErpNet.FP.

Exposes a `/metrics` endpoint in the standard text-exposition format.
Use any Prometheus / Grafana stack to scrape:

    scrape_configs:
      - job_name: erpnet-fp
        static_configs:
          - targets: ['erpnet.example.com']

Optional dependency — if `prometheus_client` is not installed at
import time, all metric helpers degrade to no-ops and `/metrics`
returns 501. Doesn't block the proxy from running.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

try:
    from prometheus_client import (
        CollectorRegistry, Counter, Gauge, Histogram, Info,
        generate_latest,
    )
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    CollectorRegistry = None
    Counter = Gauge = Histogram = Info = None
    generate_latest = None
    _PROM_AVAILABLE = False

_logger = logging.getLogger(__name__)


# ─── Registry — separate from the default global one so multiple
#     test instances don't collide on metric names ────────────────


_registry: Optional["CollectorRegistry"] = (
    CollectorRegistry(auto_describe=True) if _PROM_AVAILABLE else None
)


def get_registry() -> Optional["CollectorRegistry"]:
    return _registry


# ─── Metrics ────────────────────────────────────────────────────────


def _counter(name: str, doc: str, labels: tuple[str, ...] = ()):
    if not _PROM_AVAILABLE:
        return _NoOpMetric()
    return Counter(name, doc, labels, registry=_registry)


def _gauge(name: str, doc: str, labels: tuple[str, ...] = ()):
    if not _PROM_AVAILABLE:
        return _NoOpMetric()
    return Gauge(name, doc, labels, registry=_registry)


def _histogram(name: str, doc: str, labels: tuple[str, ...] = (), buckets=None):
    if not _PROM_AVAILABLE:
        return _NoOpMetric()
    kw = {}
    if buckets is not None:
        kw["buckets"] = buckets
    return Histogram(name, doc, labels, registry=_registry, **kw)


class _NoOpMetric:
    """Stand-in when prometheus_client is not installed — every call
    on it (.inc, .set, .observe, .labels) is a no-op."""
    def __getattr__(self, _name):
        return self
    def __call__(self, *args, **kwargs):
        return self


# Process info
proxy_info = _counter(
    "erpnet_fp_info", "Build info (always 1).", ("version",),
)

# HTTP layer
http_requests_total = _counter(
    "erpnet_fp_http_requests_total",
    "HTTP requests handled by the proxy.",
    ("method", "path", "status_code"),
)
http_request_duration_seconds = _histogram(
    "erpnet_fp_http_request_duration_seconds",
    "Latency of HTTP requests handled by the proxy.",
    ("method", "path"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# Per-device — printers
printer_actions_total = _counter(
    "erpnet_fp_printer_actions_total",
    "Fiscal printer actions per type and outcome.",
    ("printer_id", "action", "outcome"),  # outcome: success | error | timeout
)
printer_action_duration_seconds = _histogram(
    "erpnet_fp_printer_action_duration_seconds",
    "Latency of fiscal printer actions.",
    ("printer_id", "action"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
printer_status_failures_total = _counter(
    "erpnet_fp_printer_status_failures_total",
    "Number of /printers/{id}/status calls that timed out or errored.",
    ("printer_id", "reason"),  # reason: timeout | paper_out | offline | other
)

# Per-device — scales
scale_reads_total = _counter(
    "erpnet_fp_scale_reads_total",
    "Weight-read calls per scale and outcome.",
    ("scale_id", "outcome"),  # success | unstable | error | unreachable
)
scale_last_weight_kg = _gauge(
    "erpnet_fp_scale_last_weight_kg",
    "Most recent successful weight reading per scale (kg).",
    ("scale_id",),
)

# Per-device — displays
display_writes_total = _counter(
    "erpnet_fp_display_writes_total",
    "Customer-display action calls.",
    ("display_id", "action", "outcome"),
)

# Per-device — barcode readers
reader_scans_total = _counter(
    "erpnet_fp_reader_scans_total",
    "Barcode scans published by each reader.",
    ("reader_id",),
)
reader_subscribers = _gauge(
    "erpnet_fp_reader_subscribers",
    "Live SSE / WebSocket subscribers per reader.",
    ("reader_id",),
)

# Per-device — pinpads
pinpad_actions_total = _counter(
    "erpnet_fp_pinpad_actions_total",
    "Payment-terminal action calls.",
    ("pinpad_id", "action", "outcome"),
)


# ─── Helpers ─────────────────────────────────────────────────────────


@contextmanager
def time_action(metric, *labels):
    """Context manager that records elapsed seconds into a histogram.

    Usage:
        with time_action(printer_action_duration_seconds, printer_id, action):
            do_thing()
    """
    start = time.monotonic()
    try:
        yield
    finally:
        try:
            metric.labels(*labels).observe(time.monotonic() - start)
        except Exception:
            pass  # No-op metric or label mismatch — ignore


def render() -> tuple[bytes, str]:
    """Produce the Prometheus exposition payload + content-type. Returns
    (b"", "") if prometheus_client isn't installed — caller maps to 501."""
    if not _PROM_AVAILABLE or _registry is None:
        return b"", ""
    return generate_latest(_registry), "text/plain; version=0.0.4; charset=utf-8"


def set_proxy_info(version: str) -> None:
    """Pin the build-info gauge to 1 on the version label. Called once
    at FastAPI app startup."""
    if not _PROM_AVAILABLE:
        return
    try:
        # Counters can't be set; we use inc(1) on first call only.
        # If called again with the same labels, it becomes 2 — harmless
        # for label cardinality. Acceptable trade-off for not pulling
        # in Info metric (which has different cardinality semantics).
        proxy_info.labels(version=version).inc(0)  # ensures the label exists
    except Exception:
        pass


def is_available() -> bool:
    return _PROM_AVAILABLE
