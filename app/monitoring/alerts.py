"""Alert policy helpers: in-process attention items and the rule-file catalogue.

Alert *delivery* does not exist by design: rules are evaluated by Prometheus and only ever
displayed. The attention items below mirror the health-related rules in
`infra/monitoring/alert_rules.yml` so the main dashboard can show them without querying
Prometheus (the web tier never does). Nothing here can act on the bot, an exchange or config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from app.monitoring.health import MonitoringSnapshot

SEVERITIES = ("critical", "high", "warn", "info")
_RANK = {name: index for index, name in enumerate(SEVERITIES)}
SCRAPE_STALE_SECONDS = 120


@dataclass(frozen=True)
class AttentionItem:
    severity: str
    code: str
    text: str

    @property
    def rank(self) -> int:
        return _RANK.get(self.severity, len(SEVERITIES))


def evaluate_attention(
    snap: MonitoringSnapshot,
    *,
    now: datetime,
    listener_enabled: bool,
    listener_failed: bool = False,
    last_scrape_at: datetime | None,
    started_at: datetime,
    chain_stale_after: int,
) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    if not snap.db_up:
        items.append(
            AttentionItem("critical", "database_unreachable", "The database is unreachable.")
        )
    elif snap.schema_version != snap.expected_schema_version:
        items.append(
            AttentionItem(
                "critical",
                "schema_mismatch",
                "The database schema does not match this version of the code.",
            )
        )
    if snap.chain is not None and not snap.chain.ok:
        items.append(
            AttentionItem(
                "critical",
                "audit_chain_broken",
                f"The audit chain is broken at event {snap.chain.broken_at}.",
            )
        )
    elif (
        snap.db_up
        and snap.chain_verified_at is not None
        and (now - snap.chain_verified_at).total_seconds() > chain_stale_after
    ):
        items.append(
            AttentionItem(
                "warn", "audit_chain_stale", "The audit chain has not been verified recently."
            )
        )
    if listener_enabled and listener_failed:
        items.append(
            AttentionItem(
                "warn", "metrics_listener_failed", "The internal metrics listener could not start."
            )
        )
    elif listener_enabled:
        if last_scrape_at is None:
            if (now - started_at).total_seconds() > SCRAPE_STALE_SECONDS:
                items.append(
                    AttentionItem(
                        "warn",
                        "prometheus_not_scraping",
                        "Prometheus has not scraped the application yet.",
                    )
                )
        elif (now - last_scrape_at).total_seconds() > SCRAPE_STALE_SECONDS:
            items.append(
                AttentionItem(
                    "warn",
                    "prometheus_not_scraping",
                    "Prometheus has stopped scraping the application.",
                )
            )
    return sorted(items, key=lambda item: item.rank)


@dataclass(frozen=True)
class RuleInfo:
    name: str
    severity: str
    expr: str
    duration: str
    group: str


def load_rule_catalogue(path: Path) -> list[RuleInfo]:
    """Alert rules from a Prometheus rule file (used by the verifier and the docs test)."""
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules: list[RuleInfo] = []
    for group in data.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" in rule:
                rules.append(
                    RuleInfo(
                        name=rule["alert"],
                        severity=(rule.get("labels") or {}).get("severity", ""),
                        expr=str(rule["expr"]),
                        duration=str(rule.get("for", "0s")),
                        group=str(group.get("name", "")),
                    )
                )
    return rules
