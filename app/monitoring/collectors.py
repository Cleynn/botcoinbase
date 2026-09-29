"""Custom Prometheus collector and the wiring that builds the monitoring subsystem."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from app import constants
from app.config import Settings
from app.domain.enums import AuditEventType, Role
from app.domain.models import Clock
from app.monitoring.health import MonitoringService, MonitoringSnapshot
from app.monitoring.metrics import VERSION, Metrics, MetricsServer
from app.storage.database import Storage


class AppCollector(Collector):
    """Turns the cached snapshot into metrics. Series with no reliable source are omitted."""

    def __init__(self, provider: Callable[[], MonitoringSnapshot], *, mode: str) -> None:
        self._provider, self._mode = provider, mode

    def describe(self) -> Iterable[Metric]:
        return []  # unchecked collector: registration must not trigger a database read

    def collect(self) -> Iterable[Metric]:
        snap = self._provider()

        info = GaugeMetricFamily(
            "tradingdots_bot_info",
            "Build and configured mode (value is always 1).",
            labels=["version", "mode"],
        )
        info.add_metric([VERSION, self._mode], 1)
        yield info

        blocked = GaugeMetricFamily(
            "tradingdots_live_trading_blocked",
            "1 while live trading is blocked (compile-time constant).",
        )
        blocked.add_metric([], 1 if constants.LIVE_TRADING_STATUS == "BLOCKED" else 0)
        yield blocked

        up = GaugeMetricFamily("tradingdots_db_up", "1 if the database answered the last probe.")
        up.add_metric([], 1 if snap.db_up else 0)
        yield up
        expected = GaugeMetricFamily(
            "tradingdots_db_expected_schema_version", "Schema version this code requires."
        )
        expected.add_metric([], snap.expected_schema_version)
        yield expected

        if snap.db_up:
            yield _gauge(
                "tradingdots_db_probe_duration_seconds",
                "Duration of the last database probe.",
                snap.db_probe_seconds,
            )
            yield _gauge(
                "tradingdots_db_schema_version",
                "Schema version found in the database.",
                snap.schema_version,
            )
            yield _gauge(
                "tradingdots_sessions_active",
                "Sessions that are neither revoked nor expired.",
                snap.sessions_active,
            )
            if snap.users_by_role is not None:
                users = GaugeMetricFamily(
                    "tradingdots_users", "Enabled user accounts by role.", labels=["role"]
                )
                for role in Role:
                    users.add_metric([role.value], snap.users_by_role.get(role.value, 0))
                yield users
            if snap.audit_event_counts is not None:
                events = CounterMetricFamily(
                    "tradingdots_auth_events",
                    "Security audit events recorded, by event type.",
                    labels=["event"],
                )
                for (
                    event
                ) in AuditEventType:  # fixed series set, including zero, so increase() is exact
                    events.add_metric([event.value], snap.audit_event_counts.get(event.value, 0))
                yield events
            if snap.audit_events_total is not None:
                total = CounterMetricFamily(
                    "tradingdots_audit_events", "All audit events recorded."
                )
                total.add_metric([], snap.audit_events_total)
                yield total
            if snap.audit_last_event_at is not None:
                yield _gauge(
                    "tradingdots_audit_last_event_timestamp_seconds",
                    "Time of the newest audit event.",
                    snap.audit_last_event_at.timestamp(),
                )
        if snap.chain is not None:
            yield _gauge(
                "tradingdots_audit_chain_ok",
                "1 if the audit hash chain verified, 0 if it is broken.",
                1 if snap.chain.ok else 0,
            )
            yield _gauge(
                "tradingdots_audit_chain_events_verified",
                "Audit events covered by the last chain verification.",
                snap.chain.events_checked,
            )
            if snap.chain_verified_at is not None:
                yield _gauge(
                    "tradingdots_audit_last_verified_timestamp_seconds",
                    "When the audit chain was last verified.",
                    snap.chain_verified_at.timestamp(),
                )


def _gauge(name: str, doc: str, value: float | int | None) -> Metric:
    family = GaugeMetricFamily(name, doc)
    if value is not None:  # no value, no sample: never invent a zero
        family.add_metric([], value)
    return family


@dataclass
class Monitoring:
    metrics: Metrics
    service: MonitoringService
    settings: Settings

    def make_server(self) -> MetricsServer:
        cfg = self.settings.monitoring
        return MetricsServer(self.metrics, cfg.bind_address, cfg.port, cfg.allowed_scrapers)


def build_monitoring(*, storage: Storage, clock: Clock, settings: Settings) -> Monitoring:
    metrics = Metrics(clock)
    service = MonitoringService(
        storage=storage,
        clock=clock,
        auth=settings.auth,
        settings=settings.monitoring,
        on_error=metrics.record_error,
        last_scrape=lambda: metrics.last_scrape_at,
    )
    metrics.registry.register(AppCollector(service.snapshot, mode=settings.mode))
    return Monitoring(metrics, service, settings)
