"""Monitoring snapshot: one cached, read-only view of application health.

Both /metrics and the dashboard summary read this snapshot, so the database is polled at most once
per cache interval no matter how often either is requested. A database failure produces a snapshot
with `db_up=False` (never an exception): monitoring must not be able to take the application down.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import AuthSettings, MonitoringSettings
from app.domain.models import ChainStatus, Clock
from app.monitoring.alerts import AttentionItem, evaluate_attention
from app.storage.database import Storage, head_version

logger = logging.getLogger("app")


@dataclass(frozen=True)
class MonitoringSnapshot:
    taken_at: datetime
    db_up: bool
    db_probe_seconds: float | None = None
    schema_version: int | None = None
    expected_schema_version: int = 0
    sessions_active: int | None = None
    users_by_role: dict[str, int] | None = None
    audit_events_total: int | None = None
    audit_last_event_at: datetime | None = None
    audit_event_counts: dict[str, int] | None = None
    chain: ChainStatus | None = None
    chain_verified_at: datetime | None = None


@dataclass(frozen=True)
class MonitoringSummary:
    status: str
    items: tuple[AttentionItem, ...]
    rows: tuple[tuple[str, str], ...]


def format_age(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


class MonitoringService:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        auth: AuthSettings,
        settings: MonitoringSettings,
        on_error: Callable[[str], None] = lambda _name: None,
        last_scrape: Callable[[], datetime | None] = lambda: None,
    ) -> None:
        self._storage, self._clock, self._auth, self._cfg = storage, clock, auth, settings
        self._on_error, self._last_scrape = on_error, last_scrape
        self._lock = threading.Lock()
        self._cached: MonitoringSnapshot | None = None
        self._chain: ChainStatus | None = None
        self._chain_at: datetime | None = None
        self.started_at = clock.now()
        self.listener_failed = False  # set by app.main if the internal listener could not bind

    def snapshot(self, *, force: bool = False) -> MonitoringSnapshot:
        with self._lock:
            now = self._clock.now()
            cached = self._cached
            if (
                not force
                and cached
                and (now - cached.taken_at).total_seconds() < self._cfg.cache_seconds
            ):
                return cached
            self._cached = self._build(now)
            return self._cached

    def _build(self, now: datetime) -> MonitoringSnapshot:
        expected = head_version()
        started = time.perf_counter()
        try:
            with self._storage.tx() as repos:
                repos.monitoring.probe()
                probe = time.perf_counter() - started
                idle_cutoff = now - timedelta(seconds=self._auth.idle_timeout_seconds)
                snap = MonitoringSnapshot(
                    taken_at=now,
                    db_up=True,
                    db_probe_seconds=probe,
                    schema_version=repos.monitoring.schema_version(),
                    expected_schema_version=expected,
                    sessions_active=repos.monitoring.active_sessions(now, idle_cutoff),
                    users_by_role=repos.monitoring.users_by_role(),
                    audit_events_total=repos.monitoring.audit_last_seq(),
                    audit_last_event_at=repos.monitoring.audit_last_event_at(),
                    audit_event_counts=repos.monitoring.audit_counts(),
                )
                self._refresh_chain(repos, now)
        except Exception:  # noqa: BLE001  monitoring must never raise into the application
            self._on_error("database")
            logger.error("monitoring could not read database state")
            return MonitoringSnapshot(
                taken_at=now,
                db_up=False,
                expected_schema_version=expected,
                chain=self._chain,
                chain_verified_at=self._chain_at,
            )
        return MonitoringSnapshot(
            **{**snap.__dict__, "chain": self._chain, "chain_verified_at": self._chain_at}
        )

    def _refresh_chain(self, repos: object, now: datetime) -> None:
        interval = timedelta(seconds=self._cfg.chain_verify_interval_seconds)
        if self._chain_at is not None and now - self._chain_at < interval:
            return
        try:
            self._chain = repos.audit.verify_chain()  # type: ignore[attr-defined]
            self._chain_at = now
        except Exception:  # noqa: BLE001
            self._on_error("audit_chain")
            logger.error("audit chain verification failed to run")

    # -- dashboard summary -----------------------------------------------------------------
    def attention(self, snap: MonitoringSnapshot | None = None) -> list[AttentionItem]:
        snap = snap or self.snapshot()
        return evaluate_attention(
            snap,
            now=self._clock.now(),
            listener_enabled=self._cfg.enabled,
            listener_failed=self.listener_failed,
            last_scrape_at=self._last_scrape(),
            started_at=self.started_at,
            chain_stale_after=self._cfg.chain_verify_interval_seconds * 3,
        )

    def summary(self, *, detailed: bool = False) -> MonitoringSummary:
        """Dashboard summary. `detailed` (ADMIN only) adds audit event counts and positions."""
        snap = self.snapshot()
        now = self._clock.now()
        items = self.attention(snap)
        if not items:
            status = "OK"
        else:
            worst = min(items, key=lambda i: i.rank).severity
            status = f"ATTENTION ({len(items)}, worst: {worst})"
        if not snap.db_up:
            database, schema = "unreachable", "unchecked"
        else:
            database = "reachable"
            match = snap.schema_version == snap.expected_schema_version
            schema = (
                f"current (v{snap.schema_version})"
                if match
                else f"MISMATCH (v{snap.schema_version})"
            )
        if snap.chain is None:
            chain = "not verified yet"
        elif snap.chain.ok:
            chain = f"verified ({snap.chain.events_checked} events)" if detailed else "verified"
        else:
            chain = f"BROKEN at event {snap.chain.broken_at}" if detailed else "BROKEN"
        if not detailed:
            items = [
                AttentionItem(i.severity, i.code, "The audit chain is broken.")
                if i.code == "audit_chain_broken"
                else i
                for i in items
            ]
        scrape = self._last_scrape()
        if not self._cfg.enabled:
            listener, last = "disabled", "n/a"
        elif self.listener_failed:
            listener, last = "failed to start", "n/a"
        else:
            listener = "enabled (internal only)"
            last = format_age((now - scrape).total_seconds()) if scrape else "none yet"
        rows: list[tuple[str, str]] = [
            ("Monitoring status", status),
            ("Database", database),
            ("Schema", schema),
            ("Audit chain", chain),
            ("Metrics endpoint", listener),
            ("Prometheus last scrape", last),
        ]
        rows.extend((f"Attention ({i.severity})", i.text) for i in items[:5])
        return MonitoringSummary(status, tuple(items), tuple(rows))
