"""Nothing sensitive may reach /metrics, and attackers must not be able to inflate cardinality."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.domain.enums import AuditEventType, AuditResult
from app.monitoring.collectors import Monitoring
from app.monitoring.metrics import BY_FAMILY, EXTERNAL_LABELS, EXTERNAL_PREFIXES, FORBIDDEN_LABEL_RE
from tests.conftest import GOOD_PASSWORD, Account, FakeClock

CANARY = "canary-" + secrets.token_hex(6)
ALLOWED_LABEL_NAMES = (
    {label for spec in BY_FAMILY.values() for label in spec.labels}
    | {"le", "quantile"}
    | EXTERNAL_LABELS
)


@pytest.fixture
def monitoring(app: Any) -> Monitoring:
    return app.state.monitoring  # type: ignore[no-any-return]


def scrape(monitoring: Monitoring, clock: FakeClock, advance: float = 11) -> str:
    clock.advance(advance)
    return monitoring.metrics.render().decode()


def series(body: str) -> list[tuple[str, dict[str, str]]]:
    out: list[tuple[str, dict[str, str]]] = []
    for family in text_string_to_metric_families(body):
        out.extend((s.name, dict(s.labels)) for s in family.samples)
    return out


def hostile_traffic(
    client: TestClient,
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    services: Any,
) -> None:
    login(make_client(peer="203.0.113.7"), f"user-{CANARY}", f"pw-{CANARY}")
    client.get(f"/{CANARY}/secret?token={CANARY}&email=a@example.com")
    client.get(
        "/login", headers={"user-agent": f"Agent/{CANARY}", "x-forwarded-for": "198.51.100.44"}
    )
    with services.storage.tx() as repos:
        services.audit.record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            target_id=CANARY,
            reason=CANARY,
            client_tag=CANARY[:12],
            request_id=CANARY,
            detail={"note": CANARY},
        )


def test_no_secret_identity_or_request_data_appears_in_the_output(
    monitoring: Monitoring,
    clock: FakeClock,
    client: TestClient,
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    services: Any,
    admin_client: TestClient,
    admin: Account,
    viewer: Account,
    settings: Any,
    secret_key: str,
    db: Any,
) -> None:
    hostile_traffic(client, make_client, login, services)
    admin_client.get("/security")
    body = scrape(monitoring, clock)
    token = admin_client.cookies.get("__Host-td_session") or "-"
    forbidden = [
        CANARY,
        secret_key,
        GOOD_PASSWORD,
        token,
        admin.username,
        viewer.username,
        "alice@",
        "example.com",
        "203.0.113.7",
        "198.51.100.44",
        "10.0.0.5",
        "Agent/",
        settings.app_hostname,
        settings.grafana_hostname,
        db.name,
        db.app_password,
        db.ctl_password,
        "td_app",
        "td_ctl",
        "argon2",
        "postgres",
        "/home/",
        "secret?token",
        settings.cookie.name,
        "csrf_token",
        "X-CSRF",
    ]
    for value in forbidden:
        assert value not in body, f"{value!r} leaked into /metrics"
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", body.replace("0.0.4", "")), (
        "an IPv4 address leaked"
    )
    label_values = [v for _, labels in series(body) for v in labels.values()]
    assert not [
        v for v in label_values if re.fullmatch(r"[A-Za-z0-9_-]{32,}", v)
    ]  # no token- or id-shaped values
    assert not re.search(r"[0-9a-f]{32,}", body)  # no hashes or ids anywhere


def test_every_label_name_in_the_live_output_is_on_the_allowlist(
    monitoring: Monitoring, clock: FakeClock, admin_client: TestClient
) -> None:
    admin_client.get("/security")
    body = scrape(monitoring, clock)
    names = {label for _, labels in series(body) for label in labels}
    assert names <= ALLOWED_LABEL_NAMES, names - ALLOWED_LABEL_NAMES
    assert not [n for n in names if FORBIDDEN_LABEL_RE.search(n)]


def test_every_family_in_the_live_output_is_catalogued_or_from_the_process_collectors(
    monitoring: Monitoring, clock: FakeClock, admin_client: TestClient
) -> None:
    body = scrape(monitoring, clock)
    for family in text_string_to_metric_families(body):
        assert family.name in BY_FAMILY or family.name.startswith(EXTERNAL_PREFIXES), family.name


def test_label_values_are_drawn_from_small_fixed_sets(
    monitoring: Monitoring, clock: FakeClock, admin_client: TestClient
) -> None:
    admin_client.get("/security")
    values: dict[str, set[str]] = {}
    for _, labels in series(scrape(monitoring, clock)):
        for name, value in labels.items():
            values.setdefault(name, set()).add(value)
    assert values["event"] == {e.value for e in AuditEventType}
    assert values["role"] == {"ADMIN", "VIEWER"} and values["mode"] == {"BACKTEST"}
    assert values["status_class"] <= {"2xx", "3xx", "4xx", "5xx", "other"}
    assert values["collector"] <= {"database", "audit_chain", "render", "other"}
    assert all(
        v.startswith("/") or v == "unmatched" or v == "other" for v in values["route_template"]
    )


def test_attackers_cannot_inflate_metric_cardinality(
    monitoring: Monitoring,
    clock: FakeClock,
    client: TestClient,
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin_client: TestClient,
) -> None:
    baseline = len(series(scrape(monitoring, clock)))
    for i in range(120):  # distinct usernames from distinct clients
        login(
            make_client(peer=f"10.77.{i // 200}.{i % 200 + 1}"),
            f"attacker-{i:04d}",
            "wrong password entirely",
        )
    for i in range(120):  # distinct unknown paths, query strings, methods
        client.get(f"/scan/{i}/x?id={i}")
        client.post(f"/scan/{i}", data={"a": "b"})
    for i in range(60):  # distinct session cookies and hosts
        client.get(
            "/", headers={"cookie": f"__Host-td_session={i:043d}", "host": f"h{i}.example.com"}
        )
    after = len(series(scrape(monitoring, clock)))
    assert after - baseline <= 15, (
        f"series grew from {baseline} to {after}"
    )  # only fixed sets (route/status combos) may grow


def test_total_series_count_stays_small(
    monitoring: Monitoring, clock: FakeClock, admin_client: TestClient
) -> None:
    for path in ("/", "/security", "/audit", "/nope", "/login"):
        admin_client.get(path)
    assert len(series(scrape(monitoring, clock))) < 400


def test_no_component_metrics_leak_a_zero_for_unbuilt_parts(
    monitoring: Monitoring, clock: FakeClock
) -> None:
    body = scrape(monitoring, clock)
    for absent in (
        "bot_open_orders",
        "kill_switch",
        "reconciliation",
        "circuit_breaker",
        "deployed_quote",
        "protected_reserve",
        "realized_pnl",
        "llm_",
        "data_freshness",
        "order_intents",
        "fee_paid",
    ):
        assert absent not in body, absent


def test_a_future_collector_that_leaks_an_identity_label_is_neutralised(
    monitoring: Monitoring, clock: FakeClock
) -> None:
    from prometheus_client.core import GaugeMetricFamily

    class Leaky:
        def describe(self) -> list[Any]:
            return []

        def collect(self) -> list[Any]:
            fam = GaugeMetricFamily(
                "tradingdots_users", "Enabled user accounts by role.", labels=["role", "username"]
            )
            fam.add_metric(["ADMIN", "alice"], 1)
            return [fam]

    monitoring.metrics.registry.register(Leaky())
    body = scrape(monitoring, clock)
    assert "alice" not in body and 'username="' not in body


def test_the_listener_logs_nothing_about_scrapes(
    monitoring: Monitoring,
    clock: FakeClock,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.monitoring.metrics import MetricsServer
    from tests.integration.test_prometheus_scrape import http_get

    server = MetricsServer(monitoring.metrics, "127.0.0.1", 0, ["127.0.0.1/32"])
    server.start()
    try:
        for _ in range(3):
            http_get(server.address)
    finally:
        server.stop()
    out = capfd.readouterr()
    assert (
        "127.0.0.1" not in out.out + out.err + caplog.text
        and "metrics" not in (out.out + out.err).lower()
    )


def test_monitoring_configuration_files_contain_no_secrets() -> None:
    from tests.conftest import ROOT

    pattern = re.compile(
        r"(password|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9+/_-]{12,}", re.IGNORECASE
    )
    for path in (ROOT / "infra" / "monitoring").rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert not pattern.search(text), path
            assert "BEGIN " not in text or "PRIVATE KEY" not in text, path


def test_compose_passes_monitoring_secrets_only_as_required_variables(
    compose: dict[str, Any],
) -> None:
    env = compose["services"]["grafana"]["environment"]
    for key, variable in (
        ("GF_SECURITY_ADMIN_PASSWORD", "GRAFANA_ADMIN_PASSWORD"),
        ("GF_SECURITY_SECRET_KEY", "GRAFANA_SECRET_KEY"),
    ):
        assert env[key] == f"${{{variable}:?set in .env}}"
    for name in ("prometheus", "node-exporter", "cadvisor"):
        assert not any(
            re.search(r"PASSWORD|SECRET|TOKEN|KEY", str(k))
            for k in (compose["services"][name].get("environment") or {})
        )
    for name in ("prometheus", "grafana", "node-exporter", "cadvisor"):
        env = compose["services"][name].get("environment") or {}
        assert not any(
            "DB_PASSWORD" in str(k) or "TD_SECRET_KEY" in str(k) for k in env
        )  # no application secrets in monitoring
