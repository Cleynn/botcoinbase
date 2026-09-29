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
from app.domain.pairs import PairState
from app.market.candles import EVENT_CODES
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
        if snap.pair_state_counts is not None:
            counts = snap.pair_state_counts
            yield _gauge(
                "tradingdots_pair_candidates_total",
                "Pairs that are not archived (candidates and the active pair).",
                sum(n for state, n in counts.items() if state != PairState.ARCHIVED.value),
            )
            by_state = GaugeMetricFamily(
                "tradingdots_pair_state_total",
                "Pairs currently in each lifecycle state (a count, not a counter: contract name).",
                labels=["state"],
            )
            for state in PairState:  # fixed series set, zeros included: they are real counts
                by_state.add_metric([state.value], counts.get(state.value, 0))
            yield by_state
            oldest = snap.pair_oldest_verified_at
            yield _gauge(
                "tradingdots_pair_metadata_age_seconds",
                "Age of the oldest verified product metadata among non-archived pairs.",
                (snap.taken_at - oldest).total_seconds() if oldest is not None else None,
            )
        if snap.market is not None:
            m = snap.market
            yield _gauge(
                "tradingdots_ingest_last_success_timestamp_seconds",
                "When a candle import last completed without error.",
                m.last_ingest_at.timestamp() if m.last_ingest_at else None,
            )
            quality = CounterMetricFamily(
                "tradingdots_data_quality_events",
                "Data-quality events recorded by candle imports, by event code.",
                labels=["code"],
            )
            for code in EVENT_CODES:  # fixed series set, zeros included
                quality.add_metric([code], m.event_counts.get(code, 0))
            yield quality
            runs = CounterMetricFamily("tradingdots_backtest_runs", "Stored backtest runs.")
            runs.add_metric([], m.backtests)
            yield runs
            yield _gauge(
                "tradingdots_backtest_last_run_timestamp_seconds",
                "When a backtest was last stored.",
                m.last_backtest_at.timestamp() if m.last_backtest_at else None,
            )
            reports = CounterMetricFamily("tradingdots_reports", "Stored reports.")
            reports.add_metric([], m.reports)
            yield reports
            yield _gauge(
                "tradingdots_paper_running",
                "1 while the local paper session is RUNNING, else 0 (PAPER only).",
                1 if m.paper_state == "RUNNING" else 0,
            )
            orders = GaugeMetricFamily(
                "tradingdots_paper_orders",
                "Local paper orders by state (PAPER only, never exchange orders).",
                labels=["state"],
            )
            for order_state in ("OPEN", "FILLED", "CANCELLED", "REJECTED"):
                orders.add_metric([order_state], m.paper_orders.get(order_state, 0))
            yield orders
            yield _gauge(
                "tradingdots_paper_deployed_quote",
                "Paper deployment: open buy reserve plus inventory cost, in USDC (PAPER only).",
                float(m.paper_deployed),
            )
            yield _gauge(
                "tradingdots_paper_free_cash_quote",
                "Paper cash not reserved by open buys, in USDC (PAPER only).",
                float(m.paper_free_cash),
            )
            yield _gauge(
                "tradingdots_review_enabled",
                "1 if read-only review packages are enabled (disabled by default), else 0.",
                1 if m.review_enabled else 0,
            )
            review = GaugeMetricFamily(
                "tradingdots_review_packages",
                "Review packages by state (counts only, never package content).",
                labels=["state"],
            )
            for pkg_state in ("REQUESTED", "GENERATING", "READY", "FAILED", "CORRUPT", "EXPIRED"):
                review.add_metric([pkg_state], m.review_counts.get(pkg_state, 0))
            yield review
            yield _gauge(
                "tradingdots_review_last_ready_timestamp_seconds",
                "When a review package was last built.",
                m.review_last_ready.timestamp() if m.review_last_ready else None,
            )
            yield _gauge(
                "tradingdots_proposal_import_enabled",
                "1 if untrusted proposal import is enabled (disabled by default), else 0.",
                1 if m.proposal_import_enabled else 0,
            )
            proposals = GaugeMetricFamily(
                "tradingdots_proposals",
                "Imported proposals by state (counts only, never proposal content).",
                labels=["state"],
            )
            for prop_state in (
                "IMPORTED", "VALIDATING", "VALIDATED", "REJECTED", "REVIEWED",
                "CHANGE_REQUEST_CREATED", "IMPLEMENTED", "BACKTESTED", "PAPER_VALIDATED", "CLOSED",
            ):  # fmt: skip
                proposals.add_metric([prop_state], m.proposal_counts.get(prop_state, 0))
            yield proposals
            imported = CounterMetricFamily("tradingdots_llm_proposals", "Proposals ever imported.")
            imported.add_metric([], m.proposals_total)
            yield imported
            rejections = CounterMetricFamily(
                "tradingdots_llm_proposal_policy_rejections",
                "Proposals with at least one policy finding.",
            )
            rejections.add_metric([], m.proposal_policy_rejections)
            yield rejections
            counts = snap.audit_event_counts or {}
            downloads = CounterMetricFamily(
                "tradingdots_review_downloads", "Audited review package downloads."
            )
            downloads.add_metric([], counts.get("review.downloaded", 0))
            yield downloads
            denied = CounterMetricFamily(
                "tradingdots_review_denied", "Audited refusals of review package actions."
            )
            denied.add_metric([], counts.get("review.denied", 0))
            yield denied
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
