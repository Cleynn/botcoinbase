"""Metric catalogue, label policy, HTTP instruments and the internal-only /metrics listener.

Only metrics with a real, reliable source are defined. There are deliberately no order, fill,
reconciliation, kill-switch or market-data metrics: those components do not exist yet, and an
absent series is honest where a zero would be invented (docs/monitoring.md).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import prometheus_client
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import Metric
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

from app.domain.models import Clock

logger = logging.getLogger("app")

VERSION = "0.4.0"
METRICS_PATH = "/metrics"
# Classic text format: understood by every Prometheus release, including the pinned 2.53 image.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
MAX_SERIES_PER_FAMILY = 100
MAX_LABEL_VALUE = 64

prometheus_client.disable_created_metrics()  # type: ignore[no-untyped-call]


@dataclass(frozen=True)
class MetricSpec:
    name: str  # exposed name (counters end in _total)
    kind: str  # gauge | counter | histogram
    help: str
    labels: tuple[str, ...] = ()
    source: str = ""

    @property
    def family(self) -> str:
        return self.name.removesuffix("_total") if self.kind == "counter" else self.name


CATALOGUE: tuple[MetricSpec, ...] = (
    MetricSpec(
        "tradingdots_bot_info",
        "gauge",
        "Build and configured mode (value is always 1).",
        ("version", "mode"),
        "config",
    ),
    MetricSpec(
        "tradingdots_live_trading_blocked",
        "gauge",
        "1 while live trading is blocked (compile-time constant).",
        (),
        "code",
    ),
    MetricSpec(
        "tradingdots_http_requests_total",
        "counter",
        "HTTP requests handled by the web application.",
        ("route_template", "status_class"),
        "http",
    ),
    MetricSpec(
        "tradingdots_http_request_duration_seconds",
        "histogram",
        "HTTP request duration in seconds.",
        ("route_template",),
        "http",
    ),
    MetricSpec(
        "tradingdots_db_up", "gauge", "1 if the database answered the last probe.", (), "database"
    ),
    MetricSpec(
        "tradingdots_db_probe_duration_seconds",
        "gauge",
        "Duration of the last database probe.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_db_schema_version",
        "gauge",
        "Schema version found in the database.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_db_expected_schema_version",
        "gauge",
        "Schema version this code requires.",
        (),
        "code",
    ),
    MetricSpec(
        "tradingdots_sessions_active",
        "gauge",
        "Sessions that are neither revoked nor expired.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_users", "gauge", "Enabled user accounts by role.", ("role",), "database"
    ),
    MetricSpec(
        "tradingdots_auth_events_total",
        "counter",
        "Security audit events recorded, by event type.",
        ("event",),
        "database",
    ),
    MetricSpec(
        "tradingdots_audit_events_total", "counter", "All audit events recorded.", (), "database"
    ),
    MetricSpec(
        "tradingdots_audit_last_event_timestamp_seconds",
        "gauge",
        "Time of the newest audit event.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_audit_chain_ok",
        "gauge",
        "1 if the audit hash chain verified, 0 if it is broken.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_audit_chain_events_verified",
        "gauge",
        "Audit events covered by the last chain verification.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_audit_last_verified_timestamp_seconds",
        "gauge",
        "When the audit chain was last verified.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_pair_candidates_total",
        "gauge",
        "Pairs that are not archived (candidates and the active pair).",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_pair_state_total",
        "gauge",
        "Pairs currently in each lifecycle state (a count, not a counter: contract name).",
        ("state",),
        "database",
    ),
    MetricSpec(
        "tradingdots_pair_metadata_age_seconds",
        "gauge",
        "Age of the oldest verified product metadata among non-archived pairs.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_ingest_last_success_timestamp_seconds",
        "gauge",
        "When a candle import last completed without error.",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_data_quality_events_total",
        "counter",
        "Data-quality events recorded by candle imports, by event code.",
        ("code",),
        "database",
    ),
    MetricSpec(
        "tradingdots_backtest_runs_total", "counter", "Stored backtest runs.", (), "database"
    ),
    MetricSpec(
        "tradingdots_backtest_last_run_timestamp_seconds",
        "gauge",
        "When a backtest was last stored.",
        (),
        "database",
    ),
    MetricSpec("tradingdots_reports_total", "counter", "Stored reports.", (), "database"),
    MetricSpec(
        "tradingdots_paper_running",
        "gauge",
        "1 while the local paper session is RUNNING, else 0 (PAPER only).",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_paper_orders",
        "gauge",
        "Local paper orders by state (PAPER only, never exchange orders).",
        ("state",),
        "database",
    ),
    MetricSpec(
        "tradingdots_paper_deployed_quote",
        "gauge",
        "Paper deployment: open buy reserve plus inventory cost, in USDC (PAPER only).",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_paper_free_cash_quote",
        "gauge",
        "Paper cash not reserved by open buys, in USDC (PAPER only).",
        (),
        "database",
    ),
    MetricSpec(
        "tradingdots_metrics_scrapes_total",
        "counter",
        "Scrapes served by the metrics listener.",
        (),
        "listener",
    ),
    MetricSpec(
        "tradingdots_metrics_last_scrape_timestamp_seconds",
        "gauge",
        "When the metrics listener last served a scrape.",
        (),
        "listener",
    ),
    MetricSpec(
        "tradingdots_collector_errors_total",
        "counter",
        "Errors while collecting metrics, by collector.",
        ("collector",),
        "collector",
    ),
)
BY_FAMILY: dict[str, MetricSpec] = {spec.family: spec for spec in CATALOGUE}
CATALOGUE_NAMES: frozenset[str] = frozenset(spec.name for spec in CATALOGUE)

# Families emitted by prometheus_client's own collectors (process, GC, interpreter).
EXTERNAL_PREFIXES = ("process_", "python_")
EXTERNAL_LABELS = frozenset(
    {"generation", "version", "implementation", "major", "minor", "patchlevel"}
)
COLLECTOR_NAMES = ("database", "audit_chain", "render")

# Label names that must never appear on any metric (identity, secrets, free text, high cardinality).
FORBIDDEN_LABEL_RE = re.compile(
    r"user|email|(^|_)ip($|_)|client|remote|session|token|cookie|path|url|uri|agent|order|"
    r"account|password|secret|hash|key|address|host|trace|error|message|timestamp|id$",
    re.IGNORECASE,
)
_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/{}-]{0,64}$")
_LE_RE = re.compile(
    r"^(\+Inf|[0-9]+(\.[0-9]+)?(e[+-]?[0-9]+)?)$"
)  # histogram bucket bounds (incl. +Inf)
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9/_{}.-]{0,48}$")


def clamp(value: str, allowed: Iterable[str], default: str = "other") -> str:
    return value if value in set(allowed) else default


def route_label(template: str | None) -> str:
    """A route *template* (never a raw path). Anything unusual collapses to fixed values."""
    if not template:
        return "unmatched"
    return template if _ROUTE_RE.fullmatch(template) else "other"


def status_class(code: int) -> str:
    return f"{code // 100}xx" if 100 <= code <= 599 else "other"


def sanitize(families: Iterable[Metric]) -> tuple[list[Metric], list[str]]:
    """Defence in depth: drop unknown families, bad labels and runaway cardinality."""
    kept: list[Metric] = []
    dropped: list[str] = []
    for family in families:
        spec = BY_FAMILY.get(family.name)
        external = family.name.startswith(EXTERNAL_PREFIXES)
        if spec is None and not external:
            dropped.append(f"unknown_family:{family.name}")
            continue
        allowed = (
            set(spec.labels) | {"le", "quantile"}
            if spec
            else set(EXTERNAL_LABELS) | {"le", "quantile"}
        )
        clean = Metric(family.name, family.documentation, family.type, family.unit)
        series: set[frozenset[tuple[str, str]]] = set()
        for sample in family.samples:
            labels = sample.labels
            if any(name not in allowed or FORBIDDEN_LABEL_RE.search(name) for name in labels):
                dropped.append(f"bad_label:{family.name}")
                continue
            if any(
                not (_LE_RE if name == "le" else _VALUE_RE).fullmatch(str(value))
                for name, value in labels.items()
            ):
                dropped.append(f"bad_value:{family.name}")
                continue
            key = frozenset((k, v) for k, v in labels.items() if k != "le")
            if key not in series and len(series) >= MAX_SERIES_PER_FAMILY:
                dropped.append(f"cardinality:{family.name}")
                continue
            series.add(key)
            clean.samples.append(sample)
        if clean.samples or family.type in {"gauge", "counter", "histogram"} and not family.samples:
            kept.append(clean)
    return kept, dropped


class _Sanitised:
    def __init__(self, families: list[Metric]) -> None:
        self._families = families

    def collect(self) -> list[Metric]:
        return self._families


class Metrics:
    """Registry, HTTP instruments and rendering. Custom collectors are registered by the caller."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.registry = CollectorRegistry(auto_describe=False)
        self.http_requests = Counter(
            "tradingdots_http_requests_total",
            "HTTP requests handled by the web application.",
            ["route_template", "status_class"],
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "tradingdots_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ["route_template"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        )
        self.scrapes = Counter(
            "tradingdots_metrics_scrapes_total",
            "Scrapes served by the metrics listener.",
            registry=self.registry,
        )
        self.last_scrape_gauge = Gauge(
            "tradingdots_metrics_last_scrape_timestamp_seconds",
            "When the metrics listener last served a scrape.",
            registry=self.registry,
        )
        self.collector_errors = Counter(
            "tradingdots_collector_errors_total",
            "Errors while collecting metrics, by collector.",
            ["collector"],
            registry=self.registry,
        )
        for name in COLLECTOR_NAMES:  # fixed series set: increase() works from the first error
            self.collector_errors.labels(collector=name)
        ProcessCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        self._lock = threading.Lock()
        self._last_scrape_at: datetime | None = None

    # -- instruments ---------------------------------------------------------------------
    def observe_request(self, template: str | None, code: int, seconds: float) -> None:
        route = route_label(template)
        self.http_requests.labels(route_template=route, status_class=status_class(code)).inc()
        self.http_duration.labels(route_template=route).observe(max(seconds, 0.0))

    def record_error(self, collector: str) -> None:
        self.collector_errors.labels(collector=clamp(collector, COLLECTOR_NAMES)).inc()

    @property
    def last_scrape_at(self) -> datetime | None:
        with self._lock:
            return self._last_scrape_at

    # -- exposition ----------------------------------------------------------------------
    def render(self) -> bytes:
        now = self._clock.now()
        with self._lock:
            self._last_scrape_at = now
        self.scrapes.inc()
        self.last_scrape_gauge.set(now.timestamp())
        families, dropped = sanitize(self.registry.collect())
        for reason in dropped:
            logger.warning("metric output dropped: %s", reason)
        return generate_latest(_Sanitised(families))


class MetricsServer:
    """Serves only GET /metrics, only to allowed networks, on an internal address."""

    def __init__(self, metrics: Metrics, host: str, port: int, allowed: Sequence[str]) -> None:
        self._metrics = metrics
        self._host, self._port = host, port
        self._allowed = tuple(ipaddress.ip_network(item, strict=False) for item in allowed)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            raise RuntimeError("metrics listener is not running")
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        metrics, allowed = self._metrics, self._allowed

        class Handler(BaseHTTPRequestHandler):
            server_version = "metrics"
            sys_version = ""
            timeout = 10

            def _reply(
                self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8"
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _permitted(self) -> bool:
                try:
                    peer = ipaddress.ip_address(self.client_address[0])
                except ValueError:
                    return False
                return any(peer in net for net in allowed)

            def do_GET(self) -> None:
                if not self._permitted():
                    self._reply(403, b"forbidden\n")
                elif self.path.split("?", 1)[0] != METRICS_PATH:
                    self._reply(404, b"not found\n")
                else:
                    try:
                        self._reply(200, metrics.render(), CONTENT_TYPE)
                    except Exception:  # noqa: BLE001  a failing scrape must never take the app down
                        metrics.record_error("render")
                        logger.error("metrics rendering failed")
                        self._reply(500, b"error\n")

            def _method_not_allowed(self) -> None:
                self._reply(405, b"method not allowed\n")

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  never log peers
                return

        self._httpd = ThreadingHTTPServer((self._host, self._port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="metrics-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = self._thread = None


def metric_names(rendered: bytes) -> set[str]:
    """Family names in an exposition body (used by tests and the verifier)."""
    return {m.group(1).decode() for m in re.finditer(rb"^# TYPE (\S+) ", rendered, re.MULTILINE)}
