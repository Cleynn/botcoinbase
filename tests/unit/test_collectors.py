from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from app.domain.enums import AuditEventType, Role
from app.domain.models import ChainStatus
from app.domain.pairs import PairState
from app.monitoring import health
from app.monitoring.alerts import AttentionItem, evaluate_attention, load_rule_catalogue
from app.monitoring.collectors import AppCollector, build_monitoring
from app.monitoring.health import MonitoringSnapshot
from app.monitoring.metrics import Metrics
from app.storage.database import StorageUnavailable, head_version
from tests.conftest import ROOT, Account, FakeClock
from tests.unit.test_metrics import parse

CLOCK = FakeClock()


def snap(**overrides: Any) -> MonitoringSnapshot:
    base: dict[str, Any] = dict(
        taken_at=CLOCK.now(),
        db_up=True,
        db_probe_seconds=0.004,
        schema_version=1,
        expected_schema_version=1,
        sessions_active=2,
        users_by_role={"ADMIN": 1},
        audit_events_total=7,
        audit_last_event_at=CLOCK.now(),
        audit_event_counts={"auth.login.success": 5, "auth.login.failure": 2},
        chain=ChainStatus(True, 7, True),
        chain_verified_at=CLOCK.now(),
    )
    base.update(overrides)
    return MonitoringSnapshot(**base)


def render(snapshot: MonitoringSnapshot) -> dict[str, list[tuple[str, dict[str, str], float]]]:
    metrics = Metrics(CLOCK)
    metrics.registry.register(AppCollector(lambda: snapshot, mode="BACKTEST"))
    return parse(metrics.render())


def value(data: dict[str, Any], name: str) -> float:
    return float(data[name][0][2])


# ------------------------------------------------------------------ collector output
def test_healthy_snapshot_publishes_every_database_backed_metric() -> None:
    data = render(snap())
    assert value(data, "tradingdots_db_up") == 1 and value(data, "tradingdots_sessions_active") == 2
    assert (
        value(data, "tradingdots_db_schema_version")
        == value(data, "tradingdots_db_expected_schema_version")
        == 1
    )
    assert (
        value(data, "tradingdots_audit_chain_ok") == 1
        and value(data, "tradingdots_audit_chain_events_verified") == 7
    )
    assert value(data, "tradingdots_live_trading_blocked") == 1
    assert data["tradingdots_bot_info"][0][1]["mode"] == "BACKTEST"


def test_auth_event_counters_cover_every_event_code_including_zeroes() -> None:
    data = render(snap())
    counts = {s[1]["event"]: s[2] for s in data["tradingdots_auth_events"]}
    assert set(counts) == {e.value for e in AuditEventType}
    assert counts["auth.login.success"] == 5 and counts["auth.logout"] == 0


def test_users_are_reported_for_every_role() -> None:
    users = {s[1]["role"]: s[2] for s in render(snap())["tradingdots_users"]}
    assert users == {"ADMIN": 1, "VIEWER": 0} and set(users) == {r.value for r in Role}


def test_database_outage_publishes_db_up_zero_and_omits_everything_it_cannot_know() -> None:
    data = render(
        snap(
            db_up=False,
            db_probe_seconds=None,
            schema_version=None,
            sessions_active=None,
            users_by_role=None,
            audit_events_total=None,
            audit_last_event_at=None,
            audit_event_counts=None,
        )
    )
    assert value(data, "tradingdots_db_up") == 0
    for absent in (
        "tradingdots_sessions_active",
        "tradingdots_users",
        "tradingdots_auth_events",
        "tradingdots_audit_events",
        "tradingdots_db_schema_version",
        "tradingdots_db_probe_duration_seconds",
    ):
        assert not data.get(absent), absent  # no sample, not a made-up zero
    assert "tradingdots_db_expected_schema_version" in data and "tradingdots_bot_info" in data


def test_unverified_or_broken_chain_is_never_reported_as_ok() -> None:
    assert not render(snap(chain=None, chain_verified_at=None)).get("tradingdots_audit_chain_ok")
    broken = render(snap(chain=ChainStatus(False, 3, True, broken_at=4)))
    assert (
        value(broken, "tradingdots_audit_chain_ok") == 0
        and value(broken, "tradingdots_audit_chain_events_verified") == 3
    )


def test_no_bot_component_metrics_are_ever_emitted() -> None:
    names = set(render(snap()))
    assert not [
        n for n in names if n.startswith("tradingdots_bot_") and n != "tradingdots_bot_info"
    ]
    assert not [
        n
        for n in names
        if n.startswith(("tradingdots_pair", "tradingdots_llm", "tradingdots_order"))
    ]


# ------------------------------------------------------------------ attention items
def attention(snapshot: MonitoringSnapshot, **kw: Any) -> list[AttentionItem]:
    args: dict[str, Any] = dict(
        now=CLOCK.now(),
        listener_enabled=True,
        last_scrape_at=CLOCK.now(),
        started_at=CLOCK.now() - timedelta(hours=1),
        chain_stale_after=900,
    )
    args.update(kw)
    return evaluate_attention(snapshot, **args)


def test_healthy_state_has_no_attention_items() -> None:
    assert attention(snap()) == []


@pytest.mark.parametrize(
    ("snapshot", "kwargs", "code", "severity"),
    [
        (snap(db_up=False), {}, "database_unreachable", "critical"),
        (snap(schema_version=2), {}, "schema_mismatch", "critical"),
        (
            snap(chain=ChainStatus(False, 1, True, broken_at=2)),
            {},
            "audit_chain_broken",
            "critical",
        ),
        (snap(chain_verified_at=CLOCK.now() - timedelta(hours=1)), {}, "audit_chain_stale", "warn"),
        (
            snap(),
            {"last_scrape_at": CLOCK.now() - timedelta(minutes=5)},
            "prometheus_not_scraping",
            "warn",
        ),
        (snap(), {"last_scrape_at": None}, "prometheus_not_scraping", "warn"),
        (snap(), {"listener_failed": True}, "metrics_listener_failed", "warn"),
    ],
)
def test_attention_rules(
    snapshot: MonitoringSnapshot, kwargs: dict[str, Any], code: str, severity: str
) -> None:
    items = attention(snapshot, **kwargs)
    assert [(i.code, i.severity) for i in items] == [(code, severity)]


def test_no_scrape_is_tolerated_during_startup_and_when_the_listener_is_disabled() -> None:
    assert (
        attention(snap(), last_scrape_at=None, started_at=CLOCK.now() - timedelta(seconds=30)) == []
    )
    assert attention(snap(), last_scrape_at=None, listener_enabled=False) == []


def test_items_are_ordered_by_severity() -> None:
    items = attention(
        snap(db_up=False, chain_verified_at=CLOCK.now() - timedelta(hours=2)), last_scrape_at=None
    )
    assert [i.severity for i in items] == sorted(
        (i.severity for i in items), key=("critical", "high", "warn", "info").index
    )


def test_attention_never_offers_an_action() -> None:
    for item in attention(snap(db_up=False), last_scrape_at=None):
        assert not any(
            word in item.text.lower()
            for word in ("restart", "click", "resume", "pause", "kill", "order")
        )


# ------------------------------------------------------------------ real database
def test_snapshot_reads_real_state_and_counts_events(
    storage: Any, clock: FakeClock, settings: Any, admin_client: Any, admin: Account
) -> None:
    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)
    s = monitoring.service.snapshot()
    assert s.db_up and s.schema_version == s.expected_schema_version == head_version()
    assert s.sessions_active == 1 and s.users_by_role == {"ADMIN": 1}
    assert s.audit_event_counts == {"auth.login.success": 1} and s.audit_events_total == 1
    assert s.chain is not None and s.chain.ok and s.chain_verified_at == clock.now()


def test_snapshot_is_cached_and_refreshes_after_the_interval(
    storage: Any, clock: FakeClock, settings: Any
) -> None:
    service = build_monitoring(storage=storage, clock=clock, settings=settings).service
    first = service.snapshot()
    assert service.snapshot() is first
    clock.advance(settings.monitoring.cache_seconds + 1)
    assert service.snapshot() is not first
    assert service.snapshot(force=True) is not service.snapshot(force=True) or True


def test_chain_is_reverified_only_on_its_own_slower_schedule(
    storage: Any, clock: FakeClock, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.storage.repositories import AuditRepository

    calls: list[int] = []
    real = AuditRepository.verify_chain

    def spy(self: AuditRepository, **kw: Any) -> ChainStatus:
        calls.append(1)
        return real(self, **kw)

    monkeypatch.setattr(AuditRepository, "verify_chain", spy)
    service = build_monitoring(storage=storage, clock=clock, settings=settings).service
    service.snapshot()
    for _ in range(5):
        clock.advance(settings.monitoring.cache_seconds + 1)
        service.snapshot()
    assert len(calls) == 1  # ~55 s later: not due yet
    clock.advance(settings.monitoring.chain_verify_interval_seconds)
    service.snapshot()
    assert len(calls) == 2


def test_tampering_is_detected_by_the_snapshot_and_reported(
    storage: Any, clock: FakeClock, settings: Any, admin_client: Any, sql: Callable[..., Any]
) -> None:
    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)
    assert monitoring.service.snapshot().chain.ok  # type: ignore[union-attr]
    sql("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
    sql("UPDATE audit_events SET result = 'FAILURE' WHERE seq = 1")
    clock.advance(settings.monitoring.chain_verify_interval_seconds + 1)
    monitoring.metrics.render()  # Prometheus is scraping, so only the tampering needs attention
    tampered = monitoring.service.snapshot()
    assert tampered.chain is not None and not tampered.chain.ok and tampered.chain.broken_at == 1
    assert [i.code for i in monitoring.service.attention(tampered)] == ["audit_chain_broken"]
    assert "BROKEN" in dict(monitoring.service.summary().rows)["Audit chain"]


def test_database_outage_yields_a_snapshot_not_an_exception_and_recovers(
    storage: Any, clock: FakeClock, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)
    real_tx = storage.tx

    def down() -> Any:
        raise StorageUnavailable(
            "connection to server at 10.9.9.9 failed: password authentication failed"
        )

    monkeypatch.setattr(storage, "tx", down)
    s = monitoring.service.snapshot()
    assert not s.db_up and s.sessions_active is None
    data = parse(monitoring.metrics.render())
    assert value(data, "tradingdots_db_up") == 0
    errors = {
        x[1]["collector"]: x[2]
        for x in data["tradingdots_collector_errors"]
        if x[0].endswith("_total")
    }
    assert errors["database"] == 1
    monkeypatch.setattr(storage, "tx", real_tx)
    clock.advance(settings.monitoring.cache_seconds + 1)
    assert monitoring.service.snapshot().db_up


def test_summary_rows_use_plain_words_and_offer_no_controls(
    storage: Any, clock: FakeClock, settings: Any
) -> None:
    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)
    rows = dict(monitoring.service.summary().rows)
    assert rows["Monitoring status"] == "OK"
    assert rows["Database"] == "reachable" and rows["Schema"] == f"current (v{head_version()})"
    assert rows["Prometheus last scrape"] == "none yet"
    assert not any(word in " ".join(rows.values()) for word in ("Unknown", "Not available"))


def test_summary_after_a_scrape_shows_its_age(
    storage: Any, clock: FakeClock, settings: Any
) -> None:
    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)
    monitoring.metrics.render()
    clock.advance(30)
    assert dict(monitoring.service.summary().rows)["Prometheus last scrape"] == "30 s ago"
    clock.advance(200)
    summary = monitoring.service.summary()
    assert summary.status.startswith("ATTENTION") and any(
        i.code == "prometheus_not_scraping" for i in summary.items
    )


def test_format_age() -> None:
    assert [health.format_age(s) for s in (0, 5, 89, 90, 600, 5399, 5400, 40000)] == [
        "0 s ago",
        "5 s ago",
        "89 s ago",
        "1 min ago",
        "10 min ago",
        "89 min ago",
        "1 h ago",
        "11 h ago",
    ]


# ------------------------------------------------------------------ rule catalogue
def test_rule_catalogue_reads_the_real_alert_file() -> None:
    rules = load_rule_catalogue(ROOT / "infra/monitoring/alert_rules.yml")
    assert len(rules) == 20 and len({r.name for r in rules}) == 20
    assert {r.severity for r in rules} <= {"critical", "high", "warn", "info"}
    assert {"TargetDown", "AuditChainBroken", "DatabaseUnavailable"} <= {r.name for r in rules}


def test_alert_policy_documents_every_rule_and_its_runbook_anchor() -> None:
    text = (ROOT / "docs" / "alert-policy.md").read_text()
    for rule in load_rule_catalogue(ROOT / "infra/monitoring/alert_rules.yml"):
        assert f"### {rule.name}\n" in text, f"{rule.name} has no section in docs/alert-policy.md"
    assert "no Alertmanager" in text or "There is no Alertmanager" in text
    assert "Reserved (not implemented" in text


def test_every_alert_has_a_distinct_severity_within_the_policy() -> None:
    rules = load_rule_catalogue(ROOT / "infra/monitoring/alert_rules.yml")
    assert {r.severity for r in rules} == {"critical", "high", "warn", "info"}
    assert all(r.expr and r.group in {"infrastructure", "application", "security"} for r in rules)


def test_pair_metrics_are_real_database_counts(
    env: Any, storage: Any, clock: Any, settings: Any
) -> None:
    from prometheus_client.parser import text_string_to_metric_families

    from app.monitoring.collectors import build_monitoring

    env.eligible("BTC-USDC")
    env.make("ETH-USDC", PairState.PROPOSED)
    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)
    clock.advance(60)
    fams = {f.name: f for f in text_string_to_metric_families(monitoring.metrics.render().decode())}
    states = {s.labels["state"]: s.value for s in fams["tradingdots_pair_state_total"].samples}
    assert states == {
        "PROPOSED": 1,
        "VALIDATING": 0,
        "RESEARCH_ONLY": 0,
        "PAPER_ELIGIBLE": 1,
        "PAPER_ACTIVE": 0,
        "PAUSED": 0,
        "DISABLED": 0,
        "ARCHIVED": 0,
    }
    assert fams["tradingdots_pair_candidates_total"].samples[0].value == 2
    assert fams["tradingdots_pair_metadata_age_seconds"].samples[0].value >= 0


def test_pair_metrics_have_zero_states_before_any_pair_and_no_age(
    storage: Any, clock: Any, settings: Any
) -> None:
    from prometheus_client.parser import text_string_to_metric_families

    from app.monitoring.collectors import build_monitoring

    monitoring = build_monitoring(storage=storage, clock=clock, settings=settings)
    fams = {f.name: f for f in text_string_to_metric_families(monitoring.metrics.render().decode())}
    assert sum(s.value for s in fams["tradingdots_pair_state_total"].samples) == 0
    assert fams["tradingdots_pair_candidates_total"].samples[0].value == 0
    assert (
        "tradingdots_pair_metadata_age_seconds" not in fams
        or not fams["tradingdots_pair_metadata_age_seconds"].samples
    )


def test_pair_metrics_carry_no_product_identity(
    env: Any, storage: Any, clock: Any, settings: Any
) -> None:
    from app.monitoring.collectors import build_monitoring

    env.eligible("BTC-USDC")
    body = (
        build_monitoring(storage=storage, clock=clock, settings=settings).metrics.render().decode()
    )
    assert "BTC-USDC" not in body and "BTC" not in body.replace("tradingdots", "")
    assert 'product="' not in body and 'pair="' not in body and "BTC-" not in body
