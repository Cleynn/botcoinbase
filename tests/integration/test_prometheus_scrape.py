"""The metrics listener, the dashboard summary, the status script and (when the binaries exist)
real promtool/Prometheus checks against the real configuration and dashboards.

Set TD_PROMETHEUS_DIR to a directory containing `prometheus` and `promtool` (and optionally
TD_NODE_EXPORTER_DIR with `node_exporter`) to run the binary-backed tests; they skip otherwise.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.config import MonitoringSettings, Settings
from app.main import _start_metrics_listener
from app.monitoring.collectors import Monitoring
from app.monitoring.metrics import BY_FAMILY, EXTERNAL_PREFIXES, MetricsServer
from app.storage.database import StorageUnavailable, head_version
from tests.conftest import ROOT, FakeClock

MON = ROOT / "infra" / "monitoring"


# ------------------------------------------------------------------ helpers
def http_get(
    address: tuple[str, int], path: str = "/metrics", method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(*address, timeout=10)  # direct: never via a proxy
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def families(body: bytes) -> dict[str, list[Any]]:
    return {f.name: list(f.samples) for f in text_string_to_metric_families(body.decode())}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def monitoring(app: Any) -> Monitoring:
    return app.state.monitoring  # type: ignore[no-any-return]


@pytest.fixture
def listener(monitoring: Monitoring) -> Iterator[MetricsServer]:
    server = MetricsServer(monitoring.metrics, "127.0.0.1", 0, ["127.0.0.1/32"])
    server.start()
    yield server
    server.stop()


# ------------------------------------------------------------------ the listener
def test_listener_serves_valid_exposition_of_known_families_only(listener: MetricsServer) -> None:
    status, headers, body = http_get(listener.address)
    assert status == 200
    assert headers["content-type"].startswith("text/plain; version=0.0.4")
    assert headers["cache-control"] == "no-store"
    for name in families(body):
        assert name in BY_FAMILY or name.startswith(EXTERNAL_PREFIXES), name


def test_listener_does_not_advertise_its_implementation(listener: MetricsServer) -> None:
    _, headers, _ = http_get(listener.address)
    assert (
        "python" not in headers.get("server", "").lower()
        and "basehttp" not in headers.get("server", "").lower()
    )


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/healthz",
        "/metrics/",
        "/metrics/x",
        "/Metrics",
        "/../etc/passwd",
        "/docs",
        "/api/v1/query",
    ],
)
def test_only_exactly_slash_metrics_exists(listener: MetricsServer, path: str) -> None:
    assert http_get(listener.address, path)[0] == 404


def test_leading_double_slash_is_normalised_by_the_server_to_the_same_single_resource(
    listener: MetricsServer,
) -> None:
    assert (
        http_get(listener.address, "//metrics")[0] == 200
    )  # Python's http.server collapses it; nothing new is exposed


def test_query_strings_do_not_change_what_is_served(listener: MetricsServer) -> None:
    assert http_get(listener.address, "/metrics?name[]=x&debug=1")[0] == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_only_get_is_allowed(listener: MetricsServer, method: str) -> None:
    assert http_get(listener.address, "/metrics", method)[0] == 405


def test_clients_outside_the_allowed_networks_are_refused(monitoring: Monitoring) -> None:
    server = MetricsServer(monitoring.metrics, "127.0.0.1", 0, ["10.0.0.0/8"])
    server.start()
    try:
        status, _, body = http_get(server.address)
        assert status == 403 and b"tradingdots" not in body
    finally:
        server.stop()


def test_the_listener_never_logs_peers_or_requests(
    listener: MetricsServer, capfd: pytest.CaptureFixture[str]
) -> None:
    http_get(listener.address)
    http_get(listener.address, "/nope")
    out = capfd.readouterr()
    assert "127.0.0.1" not in out.out + out.err and "GET" not in out.out + out.err


def test_listener_stops_cleanly(monitoring: Monitoring) -> None:
    server = MetricsServer(monitoring.metrics, "127.0.0.1", 0, ["127.0.0.1/32"])
    server.start()
    assert server.running
    server.stop()
    assert not server.running


def test_concurrent_scrapes_are_all_served_and_counted(listener: MetricsServer) -> None:
    statuses: list[int] = []

    def scrape() -> None:
        for _ in range(5):
            statuses.append(http_get(listener.address)[0])

    threads = [threading.Thread(target=scrape) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [200] * 40
    final = families(http_get(listener.address)[2])
    assert final["tradingdots_metrics_scrapes"][0].value == 41


def test_a_scrape_is_fast(listener: MetricsServer) -> None:
    started = time.perf_counter()
    http_get(listener.address)
    assert time.perf_counter() - started < 1.0


# ------------------------------------------------------------------ what the metrics say
def test_metrics_reflect_real_application_state(
    listener: MetricsServer,
    admin_client: TestClient,
    viewer_client: TestClient,
    clock: FakeClock,
    client: TestClient,
) -> None:
    admin_client.get("/security")
    client.get("/no-such-page")
    clock.advance(11)  # let the snapshot cache expire
    data = families(http_get(listener.address)[2])
    assert data["tradingdots_db_up"][0].value == 1
    assert data["tradingdots_sessions_active"][0].value == 2
    users = {s.labels["role"]: s.value for s in data["tradingdots_users"]}
    assert users == {"ADMIN": 1, "VIEWER": 1}
    events = {s.labels["event"]: s.value for s in data["tradingdots_auth_events"]}
    assert events["auth.login.success"] == 2
    requests = {
        (s.labels["route_template"], s.labels["status_class"]): s.value
        for s in data["tradingdots_http_requests"]
        if s.name.endswith("_total")
    }
    assert (
        requests[("/login", "3xx")] == 2
        and requests[("/security", "2xx")] == 1
        and requests[("unmatched", "4xx")] == 1
    )
    assert data["tradingdots_audit_chain_ok"][0].value == 1
    assert data["tradingdots_live_trading_blocked"][0].value == 1


def test_a_database_outage_is_visible_and_recovers(
    listener: MetricsServer,
    monitoring: Monitoring,
    storage: Any,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_tx = storage.tx

    def down() -> Any:
        raise StorageUnavailable("db down")

    monkeypatch.setattr(storage, "tx", down)
    clock.advance(11)
    data = families(http_get(listener.address)[2])
    assert data["tradingdots_db_up"][0].value == 0 and "tradingdots_sessions_active" not in data
    monkeypatch.setattr(storage, "tx", real_tx)
    clock.advance(11)
    assert families(http_get(listener.address)[2])["tradingdots_db_up"][0].value == 1


def test_the_web_application_itself_never_serves_metrics(
    app: Any, client: TestClient, admin_client: TestClient
) -> None:
    for c in (client, admin_client):
        for path in ("/metrics", "/metrics/", "/-/metrics", "/prometheus", "/internal/metrics"):
            assert c.get(path, follow_redirects=False).status_code == 404
    assert not [r for r in app.router.routes if "metric" in getattr(r, "path", "")]


# ------------------------------------------------------------------ listener start-up failure
def test_a_failed_bind_does_not_stop_the_application(
    settings: Settings, app: Any, admin_client: TestClient
) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        busy = settings.model_copy(
            update={"monitoring": MonitoringSettings(bind_address="127.0.0.1", port=port)}
        )
        app.state.monitoring.settings = busy
        assert _start_metrics_listener(app, busy) is None
    assert app.state.monitoring.service.listener_failed
    rows = dict(app.state.monitoring.service.summary().rows)
    assert rows["Metrics endpoint"] == "failed to start"
    page = admin_client.get("/")
    assert page.status_code == 200 and "could not start" in page.text


def test_the_listener_starts_when_the_address_is_free(settings: Settings, app: Any) -> None:
    free = settings.model_copy(
        update={"monitoring": MonitoringSettings(bind_address="127.0.0.1", port=free_port())}
    )
    app.state.monitoring.settings = free
    server = _start_metrics_listener(app, free)
    try:
        assert server is not None and http_get(server.address)[0] == 200
    finally:
        server.stop()


def test_a_disabled_listener_starts_nothing(settings: Settings, app: Any) -> None:
    off = settings.model_copy(update={"monitoring": MonitoringSettings(enabled=False)})
    assert _start_metrics_listener(app, off) is None


# ------------------------------------------------------------------ dashboard summary
def test_dashboard_shows_a_simple_monitoring_summary_and_the_grafana_link(
    admin_client: TestClient,
) -> None:
    body = admin_client.get("/").text
    known = body.split("Known values")[1].split("Not available yet")[0]
    for label in (
        "Monitoring status",
        "Database",
        "Schema",
        "Audit chain",
        "Metrics endpoint",
        "Prometheus last scrape",
    ):
        assert label in known, label
    assert f"current (v{head_version()})" in known and "verified (1 events)" in known
    assert 'href="https://grafana.tradingdots.onthewall.ovh/"' in body
    assert not any(tag in body.lower() for tag in ("<iframe", "<embed", "<object"))


def test_viewers_see_the_summary_without_audit_detail(
    viewer_client: TestClient, sql: Callable[..., Any], clock: FakeClock, settings: Settings
) -> None:
    body = viewer_client.get("/").text
    known = body.split("Known values")[1].split("Not available yet")[0]
    assert "Monitoring status" in known and "<dd>verified</dd>" in known
    assert "events)" not in known and "BROKEN at event" not in known
    sql("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
    sql("UPDATE audit_events SET result = 'DENIED' WHERE seq = 1")
    clock.advance(settings.monitoring.chain_verify_interval_seconds + 1)
    broken = viewer_client.get("/").text
    assert (
        "<dd>BROKEN</dd>" in broken
        and "The audit chain is broken." in broken
        and "event 1" not in broken
    )


def test_summary_visible_to_viewers_and_adds_no_controls(viewer_client: TestClient) -> None:
    body = viewer_client.get("/").text
    assert "Monitoring status" in body
    assert re.findall(r'<form[^>]*action="([^"]+)"', body) == ["/logout"]


def test_summary_reflects_a_scrape_and_a_broken_chain(
    admin_client: TestClient,
    listener: MetricsServer,
    clock: FakeClock,
    sql: Callable[..., Any],
    settings: Settings,
) -> None:
    assert "none yet" in admin_client.get("/").text
    http_get(listener.address)
    clock.advance(20)
    assert "20 s ago" in admin_client.get("/").text
    sql("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
    sql("UPDATE audit_events SET result = 'DENIED' WHERE seq = 1")
    clock.advance(settings.monitoring.chain_verify_interval_seconds + 1)
    http_get(listener.address)
    body = admin_client.get("/").text
    assert (
        "ATTENTION" in body
        and "BROKEN at event 1" in body
        and "The audit chain is broken at event 1." in body
    )


def test_a_monitoring_failure_never_breaks_the_dashboard(
    admin_client: TestClient, monitoring: Monitoring, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Any:
        raise RuntimeError("monitoring exploded password=canary-xyz")

    monkeypatch.setattr(monitoring.service, "summary", boom)
    response = admin_client.get("/")
    assert response.status_code == 200 and "Signed in as" in response.text
    assert "Monitoring status" not in response.text and "canary-xyz" not in response.text


def test_alerts_tile_stays_honest_about_where_alert_state_lives(admin_client: TestClient) -> None:
    unavailable = admin_client.get("/").text.split("Not available yet")[1]
    assert "Alerts" in unavailable  # alert state lives in Prometheus; the web tier never queries it


# ------------------------------------------------------------------ monitoring_status script
def _load_status() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "monitoring_status", ROOT / "scripts" / "monitoring_status.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TARGETS = {
    "data": {
        "activeTargets": [
            {"labels": {"job": "prometheus"}, "health": "up", "lastError": ""},
            {"labels": {"job": "tradingdots-app"}, "health": "up", "lastError": ""},
            {"labels": {"job": "node"}, "health": "up", "lastError": ""},
            {"labels": {"job": "cadvisor"}, "health": "down", "lastError": "connection refused"},
        ]
    }
}
NO_ALERTS: dict[str, Any] = {"data": {"alerts": []}}


def fake_runner(
    targets: dict[str, Any],
    alerts: dict[str, Any],
    calls: list[list[str]] | None = None,
    fail: bool = False,
) -> Callable[..., Any]:
    def run(command: Any) -> subprocess.CompletedProcess[str]:
        command = list(command)
        if calls is not None:
            calls.append(command)
        if fail:
            return subprocess.CompletedProcess(command, 1, "", "boom")
        url = command[-1]
        out = (
            "Prometheus Server is Healthy.\n"
            if url.endswith("/-/healthy")
            else json.dumps(targets if "targets" in url else alerts)
        )
        return subprocess.CompletedProcess(command, 0, out, "")

    return run


def test_status_script_ignores_the_optional_cadvisor_when_it_is_down(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = _load_status()
    assert status.main([], runner=fake_runner(TARGETS, NO_ALERTS)) == 0
    assert "STATUS: OK" in capsys.readouterr().out


def test_status_script_flags_a_down_target_and_serious_alerts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = _load_status()
    down = json.loads(json.dumps(TARGETS))
    down["data"]["activeTargets"][1]["health"] = "down"
    assert status.main([], runner=fake_runner(down, NO_ALERTS)) == 1
    alerts = {
        "data": {
            "alerts": [
                {
                    "labels": {"alertname": "DatabaseUnavailable", "severity": "critical"},
                    "state": "firing",
                    "activeAt": "t",
                },
                {"labels": {"alertname": "X", "severity": "critical"}, "state": "pending"},
            ]
        }
    }
    capsys.readouterr()
    assert status.main(["--json"], runner=fake_runner(TARGETS, alerts)) == 1
    summary = json.loads(capsys.readouterr().out)
    assert [f["alert"] for f in summary["firing"]] == [
        "DatabaseUnavailable"
    ]  # the pending one is not firing


def test_status_script_treats_warn_alerts_as_informational() -> None:
    status = _load_status()
    warn = {
        "data": {
            "alerts": [
                {
                    "labels": {"alertname": "DiskUsageWarn", "severity": "warn"},
                    "state": "firing",
                    "activeAt": "t",
                }
            ]
        }
    }
    assert status.main([], runner=fake_runner(TARGETS, warn)) == 0


def test_status_script_only_issues_allowlisted_read_only_requests() -> None:
    status = _load_status()
    calls: list[list[str]] = []
    status.main([], runner=fake_runner(TARGETS, NO_ALERTS, calls))
    assert len(calls) == 3
    for command in calls:
        assert command[:6] == ["docker", "compose", "exec", "-T", "prometheus", "wget"]
        assert command[-1].startswith("http://127.0.0.1:9090/") and not any(
            x in command for x in ("-X", "--post-data", "--method")
        )
        assert command[-1].removeprefix("http://127.0.0.1:9090") in status.ALLOWED_PATHS
    with pytest.raises(ValueError):
        status.fetch("/-/reload", ["docker", "compose"])
    with pytest.raises(ValueError):
        status.fetch("/api/v1/admin/tsdb/delete_series", ["docker", "compose"])


def test_status_script_reports_an_unreachable_stack_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = _load_status()
    assert status.main([], runner=fake_runner(TARGETS, NO_ALERTS, fail=True)) == 2
    captured = capsys.readouterr()
    assert "could not query Prometheus" in captured.err and "Traceback" not in captured.err


# ------------------------------------------------------------------ real binaries (optional)
def _tool(env: str, name: str) -> Path | None:
    directory = os.environ.get(env)
    candidates = [Path(directory) / name] if directory else []
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    return next((c for c in candidates if c.exists()), None)


prometheus_bin = _tool("TD_PROMETHEUS_DIR", "prometheus")
promtool_bin = _tool("TD_PROMETHEUS_DIR", "promtool")
needs_promtool = pytest.mark.skipif(
    promtool_bin is None, reason="promtool not found (set TD_PROMETHEUS_DIR)"
)


def _staged_config(tmp_path: Path, **targets: str) -> Path:
    """The real prometheus.yml with container paths/targets remapped to this checkout."""
    cfg = yaml.safe_load((MON / "prometheus.yml").read_text())
    cfg["rule_files"] = [str(MON / "recording_rules.yml"), str(MON / "alert_rules.yml")]
    for job in cfg["scrape_configs"]:
        for static in job.get("static_configs", []):
            static["targets"] = [targets.get(job["job_name"], t) for t in static["targets"]]
    path = tmp_path / "prometheus.yml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@needs_promtool
def test_promtool_accepts_the_real_config_and_rules(tmp_path: Path) -> None:
    assert promtool_bin
    config = _staged_config(tmp_path)
    for args in (
        ["check", "config", str(config)],
        ["check", "rules", str(MON / "recording_rules.yml"), str(MON / "alert_rules.yml")],
    ):
        result = subprocess.run(
            [str(promtool_bin), *args], capture_output=True, text=True, check=False
        )  # noqa: S603
        assert result.returncode == 0, result.stdout + result.stderr
    assert "SUCCESS" in result.stdout


def _rule_test(
    tmp_path: Path, name: str, series: dict[str, str], checks: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    assert promtool_bin
    shutil.copy(MON / "alert_rules.yml", tmp_path / "alert_rules.yml")
    shutil.copy(MON / "recording_rules.yml", tmp_path / "recording_rules.yml")
    doc = {
        "rule_files": ["recording_rules.yml", "alert_rules.yml"],
        "evaluation_interval": "15s",
        "tests": [
            {
                "name": name,
                "interval": "15s",
                "input_series": [{"series": s, "values": v} for s, v in series.items()],
                "alert_rule_test": checks,
            }
        ],
    }
    (tmp_path / "test.yml").write_text(yaml.safe_dump(doc))
    return subprocess.run(
        [str(promtool_bin), "test", "rules", str(tmp_path / "test.yml")],
        capture_output=True,
        text=True,
        check=False,
    )  # noqa: S603


def _ann(rule: str, **labels: str) -> dict[str, str]:
    for group in yaml.safe_load((MON / "alert_rules.yml").read_text())["groups"]:
        for r in group["rules"]:
            if r["alert"] == rule:
                return {
                    k: re.sub(r"\{\{ \$labels\.(\w+) \}\}", lambda m: labels[m.group(1)], v)
                    for k, v in r["annotations"].items()
                }
    raise KeyError(rule)


def _fires(rule: str, at: str, labels: dict[str, str], **ann: str) -> dict[str, Any]:
    return {
        "eval_time": at,
        "alertname": rule,
        "exp_alerts": [{"exp_labels": labels, "exp_annotations": _ann(rule, **ann)}],
    }


def _quiet(rule: str, at: str) -> dict[str, Any]:
    return {"eval_time": at, "alertname": rule, "exp_alerts": []}


@needs_promtool
def test_target_down_fires_after_a_minute_and_only_for_required_jobs(tmp_path: Path) -> None:
    checks = [
        _quiet("TargetDown", "30s"),
        _fires(
            "TargetDown",
            "2m",
            {
                "severity": "critical",
                "component": "monitoring",
                "job": "tradingdots-app",
                "instance": "app:9464",
            },
            job="tradingdots-app",
        ),
    ]
    series = {
        'up{job="tradingdots-app",instance="app:9464"}': "1 1 0 0 0 0 0 0 0 0 0 0",
        'up{job="cadvisor",instance="cadvisor:8080"}': "0x11",
    }  # optional job: must never alert
    result = _rule_test(tmp_path, "target down", series, checks)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_promtool
def test_database_and_schema_alerts(tmp_path: Path) -> None:
    series = {
        "tradingdots_db_up": "1 1 0 0 0 0 0 0",
        "tradingdots_db_schema_version": "1x8",
        "tradingdots_db_expected_schema_version": "1 1 1 2 2 2 2 2",
    }
    checks = [
        _quiet("DatabaseUnavailable", "30s"),
        _fires("DatabaseUnavailable", "2m", {"severity": "critical", "component": "app"}),
        _quiet("SchemaMismatch", "30s"),
        _fires("SchemaMismatch", "2m", {"severity": "critical", "component": "app"}),
    ]
    result = _rule_test(tmp_path, "db", series, checks)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_promtool
def test_audit_chain_alerts(tmp_path: Path) -> None:
    series = {
        "tradingdots_audit_chain_ok": "1 1 0 0",
        "tradingdots_audit_last_verified_timestamp_seconds": "0x400",
    }
    checks = [
        _quiet("AuditChainBroken", "15s"),
        _fires("AuditChainBroken", "45s", {"severity": "critical", "component": "security"}),
        _quiet("AuditChainVerifyStale", "20m"),
        _fires("AuditChainVerifyStale", "40m", {"severity": "warn", "component": "security"}),
    ]
    result = _rule_test(tmp_path, "audit", series, checks)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_promtool
def test_http_error_ratio_and_healthy_traffic(tmp_path: Path) -> None:
    bad = {
        'tradingdots_http_requests_total{route_template="/",status_class="5xx"}': "0+2x80",
        'tradingdots_http_requests_total{route_template="/",status_class="2xx"}': "0+2x80",
    }
    result = _rule_test(
        tmp_path,
        "5xx",
        bad,
        [
            _quiet("Http5xxRatioHigh", "2m"),
            _fires("Http5xxRatioHigh", "15m", {"severity": "high", "component": "app"}),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    good = {'tradingdots_http_requests_total{route_template="/",status_class="2xx"}': "0+5x80"}
    result = _rule_test(tmp_path, "healthy", good, [_quiet("Http5xxRatioHigh", "15m")])
    assert result.returncode == 0, result.stdout + result.stderr


@needs_promtool
def test_disk_alerts_escalate_by_severity(tmp_path: Path) -> None:
    labels = 'mountpoint="/",fstype="ext4"'
    series = {
        f"node_filesystem_size_bytes{{{labels}}}": "100x80",
        f"node_filesystem_avail_bytes{{{labels}}}": "25x80",
    }
    result = _rule_test(
        tmp_path,
        "disk 75%",
        series,
        [
            _quiet("DiskUsageWarn", "5m"),
            _fires(
                "DiskUsageWarn",
                "15m",
                {"severity": "warn", "component": "node", "mountpoint": "/", "fstype": "ext4"},
            ),
            _quiet("DiskUsageHigh", "15m"),
            _quiet("DiskUsageCritical", "15m"),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    series = {
        f"node_filesystem_size_bytes{{{labels}}}": "100x80",
        f"node_filesystem_avail_bytes{{{labels}}}": "5x80",
    }
    result = _rule_test(
        tmp_path,
        "disk 95%",
        series,
        [
            _fires(
                "DiskUsageCritical",
                "15m",
                {"severity": "critical", "component": "node", "mountpoint": "/", "fstype": "ext4"},
            )
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_promtool
def test_security_spike_alerts(tmp_path: Path) -> None:
    series = {
        'tradingdots_auth_events_total{event="auth.login.failure"}': "0+3x100",
        'tradingdots_auth_events_total{event="auth.login.throttled"}': "0x100",
    }
    result = _rule_test(
        tmp_path,
        "auth spike",
        series,
        [
            _fires(
                "AuthFailureSpike",
                "20m",
                {"severity": "warn", "component": "security", "event": "auth.login.failure"},
            ),
            _quiet("LoginThrottleActive", "20m"),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _api(port: int, path: str) -> dict[str, Any]:
    status, _, body = http_get(("127.0.0.1", port), path)
    assert status == 200, (path, status, body[:200])
    result: dict[str, Any] = json.loads(body)
    return result


def _dashboard_queries() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted((MON / "grafana/dashboards").glob("*.json")):
        doc = json.loads(path.read_text())
        for panel in doc["panels"]:
            for target in panel.get("targets", []):
                expr = target["expr"]
                for macro, value in (("$__rate_interval", "5m"), ("$__interval", "1m")):
                    expr = expr.replace(macro, value)
                out.append((f"{path.name}/{panel['title']}", expr))
    return out


@pytest.mark.skipif(prometheus_bin is None, reason="prometheus not found (set TD_PROMETHEUS_DIR)")
def test_end_to_end_with_real_prometheus(
    tmp_path: Path,
    monitoring: Monitoring,
    admin_client: TestClient,
    viewer_client: TestClient,
    clock: FakeClock,
) -> None:
    """Real Prometheus scrapes the real listener (real PostgreSQL) and node_exporter, evaluates the
    real rules, and every dashboard query executes without error."""
    assert prometheus_bin
    node_bin = _tool("TD_NODE_EXPORTER_DIR", "node_exporter")
    app_port, prom_port, node_port = free_port(), free_port(), free_port()
    server = MetricsServer(monitoring.metrics, "127.0.0.1", app_port, ["127.0.0.1/32"])
    server.start()
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(3):
            admin_client.get("/security")
        clock.advance(11)
        targets = {
            "tradingdots-app": f"127.0.0.1:{app_port}",
            "prometheus": f"127.0.0.1:{prom_port}",
        }
        if node_bin:
            targets["node"] = f"127.0.0.1:{node_port}"
            procs.append(
                subprocess.Popen(
                    [str(node_bin), f"--web.listen-address=127.0.0.1:{node_port}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )  # noqa: S603
        config = _staged_config(tmp_path, **targets)
        procs.append(
            subprocess.Popen(  # noqa: S603
                [
                    str(prometheus_bin),
                    f"--config.file={config}",
                    f"--storage.tsdb.path={tmp_path / 'tsdb'}",
                    f"--web.listen-address=127.0.0.1:{prom_port}",
                    "--storage.tsdb.retention.time=30d",
                    "--storage.tsdb.retention.size=15GB",
                    "--no-web.enable-lifecycle",
                    "--no-web.enable-admin-api",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        wanted = {"tradingdots-app", "prometheus"} | ({"node"} if node_bin else set())
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                active = _api(prom_port, "/api/v1/targets?state=active")["data"]["activeTargets"]
                if {t["labels"]["job"] for t in active if t["health"] == "up"} >= wanted:
                    break
            except (OSError, AssertionError):
                pass
            time.sleep(2)
        else:
            pytest.fail("Prometheus did not reach all targets in 60 s")

        def instant(query: str) -> list[Any]:
            from urllib.parse import quote

            doc = _api(prom_port, f"/api/v1/query?query={quote(query)}")
            assert doc["status"] == "success", (query, doc)
            result: list[Any] = doc["data"]["result"]
            return result

        assert [r["value"][1] for r in instant('up{job="tradingdots-app"}')] == ["1"]
        assert instant("tradingdots_live_trading_blocked")[0]["value"][1] == "1"
        assert instant("tradingdots_bot_info")[0]["metric"]["mode"] == "BACKTEST"
        assert instant("tradingdots_db_up")[0]["value"][1] == "1"
        assert instant("tradingdots_audit_chain_ok")[0]["value"][1] == "1"
        # Series the collectors must NOT produce because the components do not exist.
        assert (
            instant(
                '{__name__=~"tradingdots_bot_(open_orders|kill_switch_active|reconciliation_age_seconds)"}'
            )
            == []
        )

        time.sleep(35)  # two 15 s evaluation cycles so rates and recording rules have data
        recorded = [
            "tradingdots:http_requests:rate5m",
            "tradingdots:http_request_duration_seconds:p95_5m",
        ]
        if node_bin:
            recorded += [
                "node:cpu_utilisation:ratio5m",
                "node:memory_utilisation:ratio",
                "node:filesystem_utilisation:ratio",
            ]
        for name in recorded:
            assert instant(name), f"recording rule {name} produced nothing"
        assert instant("increase(prometheus_rule_evaluation_failures_total[5m]) > 0") == []
        rules = _api(prom_port, "/api/v1/rules")["data"]["groups"]
        assert all(r.get("health") == "ok" for g in rules for r in g["rules"]), (
            "a rule failed to evaluate"
        )
        assert len([r for g in rules for r in g["rules"] if r["type"] == "alerting"]) == 20
        firing = [
            a for a in _api(prom_port, "/api/v1/alerts")["data"]["alerts"] if a["state"] == "firing"
        ]
        assert not [a for a in firing if a["labels"].get("severity") == "critical"], firing
        for _where, expr in _dashboard_queries():
            instant(expr)  # raises on any evaluation error
        # Prometheus is read-only here: lifecycle calls are forbidden and the admin API is disabled.
        for method, path in (("POST", "/-/reload"), ("PUT", "/-/quit")):
            assert http_get(("127.0.0.1", prom_port), path, method)[0] == 403, (method, path)
        for method, path in (
            ("POST", "/api/v1/admin/tsdb/snapshot"),
            ("POST", "/api/v1/admin/tsdb/delete_series?match[]=up"),
            ("PUT", "/api/v1/admin/tsdb/clean_tombstones"),
        ):
            status, _, body = http_get(("127.0.0.1", prom_port), path, method)
            assert status >= 400 and b"admin APIs disabled" in body, (
                method,
                path,
                status,
                body[:120],
            )
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        server.stop()
