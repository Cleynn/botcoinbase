"""The safety monitor: turns recorded facts into a circuit-breaker decision. Host side.

`tick` reads the latest reconciliation findings, API failures, loss and drawdown, intent rate and
reject streak, asks the pure breaker policy whether to open, and if so opens the breaker (which
pauses the bot). It never cancels, sells or resumes anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Final

from app.config import Settings
from app.domain.models import Clock
from app.safety import anomaly, breaker, ledger
from app.safety.context import expected_state
from app.safety.host_control import HostControl
from app.storage.database import Storage
from app.storage.repositories import Repos

VENUES: Final = ("PAPER", "FAKE", "COINBASE")
_UTC = timezone.utc  # noqa: UP017


@dataclass(frozen=True)
class MonitorResult:
    tripped: str | None
    signals: dict[str, object] = field(default_factory=dict)


class SafetyMonitor:
    def __init__(self, *, storage: Storage, clock: Clock, settings: Settings, host: HostControl):
        self._storage, self._clock, self._settings, self._host = storage, clock, settings, host

    def signals(self, repos: Repos, now: datetime) -> breaker.BreakerSignals:
        s = self._settings.safety
        window = now - timedelta(seconds=s.api_failure_window_seconds)
        failures = max((repos.safety.api_failures_since(v, window) for v in VENUES), default=0)
        consecutive = max((repos.safety.consecutive_bad_runs(v) for v in VENUES), default=0)
        unknown_order = False
        anomalies: list[anomaly.Anomaly] = []
        run = repos.safety.latest_run()
        if run is not None:
            for f in repos.safety.findings(run.id):
                if f.code in ("UNKNOWN_ORDER", "ORDER_APPEARED_AFTER_ABSENCE"):
                    unknown_order = True
                elif f.code == "BALANCE_MISMATCH":
                    anomalies.append(anomaly.Anomaly("BALANCE_MISMATCH", "TRIP"))
                elif f.code == "FILL_ANOMALY":
                    code = (
                        f.detail if f.detail in breaker.TRIP_REASONS else "TAKER_FILL_ON_POST_ONLY"
                    )
                    anomalies.append(anomaly.Anomaly(str(code), "TRIP"))
        rate = anomaly.order_rate(
            repos.safety.intents_since(now - timedelta(minutes=1)), s.max_intents_per_minute
        )
        if rate:
            anomalies.append(rate)
        streak = 0
        for recent in repos.safety.recent_attempt_states(s.max_reject_streak):
            if recent != "REJECTED":
                break
            streak += 1
        storm = anomaly.reject_storm(streak, s.max_reject_streak)
        if storm:
            anomalies.append(storm)
        loss = drawdown = False
        for venue in VENUES:
            baselines = repos.safety.baselines(venue)
            if ledger.QUOTE not in baselines:
                continue
            replayed = expected_state(repos, venue)
            current, peak, day_open = ledger.equity_view(
                replayed,
                baselines[ledger.QUOTE],
                day_start=datetime.combine(now.date(), time.min, tzinfo=_UTC),
                current_marks={},
            )
            loss = loss or (day_open - current >= s.daily_loss_limit)
            if peak > Decimal(0) and (peak - current) / peak >= s.max_drawdown_ratio:
                drawdown = True
        return breaker.BreakerSignals(
            recent_api_failures=failures,
            consecutive_reconcile_failures=consecutive,
            unknown_order_found=unknown_order,
            loss_limit_hit=loss,
            drawdown_hit=drawdown,
            anomalies=tuple(anomalies),
        )

    def tick(self) -> MonitorResult:
        now = self._clock.now()
        with self._storage.tx() as repos:
            sig = self.signals(repos, now)
        reason = breaker.evaluate(sig, self._settings.safety)
        summary: dict[str, object] = {
            "api_failures": sig.recent_api_failures,
            "reconcile_failures": sig.consecutive_reconcile_failures,
            "unknown_order": sig.unknown_order_found,
            "loss": sig.loss_limit_hit,
            "drawdown": sig.drawdown_hit,
            "anomalies": [a.code for a in sig.anomalies],
        }
        if reason is not None:
            self._host.trip_breaker(reason)
        return MonitorResult(reason, summary)
