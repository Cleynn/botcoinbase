"""The Phase 8 metric families: aggregate counts only, fixed label sets, nothing invented."""

from __future__ import annotations

from app.monitoring import metrics as m
from app.monitoring.collectors import (
    FAILURE_CLASSES,
    ORDER_EVENT_TYPES,
    REASON_CLASSES,
    _safety_families,
)
from app.monitoring.health import SafetyFacts
from app.safety.types import BLOCK_REASONS
from app.storage.safety_repositories import SafetyStats


def facts(**over: object) -> SafetyFacts:
    stats = SafetyStats(
        reasons={"KILL_SWITCH_ACTIVE": 2, "STALE_MARKET_DATA": 1, "RESERVE_BREACH": 3},
        decisions={"ALLOW": 4, "BLOCK": 6},
        attempts={"WORKING": 2, "CANCEL_REQUESTED": 1, "FILLED": 5},
        api={
            ("list_orders", True, ""): 10,
            ("list_orders", False, "TIMEOUT"): 2,
            ("submit", False, "RATE_LIMITED"): 1,
        },
        findings=3,
        fill_anomalies=1,
        events={"order.submitted": 4, "order.unknown": 1},
    )
    base = dict(
        kill_active=True,
        breaker_open=False,
        running=False,
        recovery_complete=True,
        reconciliation_age_seconds=42.0,
        stats=stats,
    )
    base.update(over)
    return SafetyFacts(**base)  # type: ignore[arg-type]


def sample_map() -> dict[str, dict[tuple[tuple[str, str], ...], float]]:
    out: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
    for family in _safety_families(facts()):
        for sample in family.samples:
            out.setdefault(sample.name, {})[tuple(sorted(sample.labels.items()))] = sample.value
    return out


def test_values_are_the_recorded_counts() -> None:
    s = sample_map()
    assert (
        s["tradingdots_bot_kill_switch_active"][()] == 1
        and s["tradingdots_bot_circuit_breaker_state"][()] == 0
    )
    assert s["tradingdots_bot_running"][()] == 0 and s["tradingdots_bot_recovery_complete"][()] == 1
    assert s["tradingdots_bot_reconciliation_age_seconds"][()] == 42
    assert s["tradingdots_bot_reconciliation_mismatches_total"][()] == 3
    assert s["tradingdots_bot_open_orders"][()] == 3
    assert s["tradingdots_bot_fill_anomalies_total"][()] == 1
    assert s["tradingdots_bot_order_intents_total"][(("result", "allowed"),)] == 4
    assert s["tradingdots_bot_order_intents_total"][(("result", "blocked"),)] == 6
    rej = s["tradingdots_bot_risk_rejections_total"]
    assert (
        rej[(("reason_class", "authority"),)] == 2
        and rej[(("reason_class", "data"),)] == 1
        and rej[(("reason_class", "capital"),)] == 3
    )
    assert (
        s["tradingdots_bot_api_requests_total"][
            (("endpoint_class", "read"), ("status_class", "ok"))
        ]
        == 10
    )
    assert (
        s["tradingdots_bot_api_errors_total"][
            (("endpoint_class", "write"), ("failure_class", "rate_limited"))
        ]
        == 1
    )
    assert s["tradingdots_bot_order_events_total"][(("event_type", "submitted"),)] == 4


def test_the_reconciliation_age_is_absent_until_one_has_run() -> None:
    names = {f.name for f in _safety_families(facts(reconciliation_age_seconds=None)) if f.samples}
    assert "tradingdots_bot_reconciliation_age_seconds" not in names


def test_every_family_is_in_the_catalogue_and_survives_the_sanitiser() -> None:
    families = list(_safety_families(facts()))
    kept, dropped = m.sanitize(families)
    assert dropped == [] and {f.name for f in kept} == {f.name for f in families}


def test_label_sets_are_fixed_and_no_reason_or_id_leaks() -> None:
    values: dict[str, set[str]] = {}
    for family in _safety_families(facts()):
        for sample in family.samples:
            for name, value in sample.labels.items():
                values.setdefault(name, set()).add(value)
    assert values["reason_class"] == set(REASON_CLASSES)
    assert values["failure_class"] == set(FAILURE_CLASSES)
    assert values["event_type"] == set(ORDER_EVENT_TYPES)
    assert not (
        set().union(*values.values()) & set(BLOCK_REASONS)
    )  # raw reason codes never label a series
