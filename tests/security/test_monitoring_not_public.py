"""Nothing monitoring-related is public, monitoring is read-only, and Grafana is hardened."""

from __future__ import annotations

import copy
import importlib.util
import ipaddress
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from app.config import ConfigError, load_settings, monitoring_problems
from tests.conftest import ROOT

MON = ROOT / "infra" / "monitoring"
MONITORING = ("prometheus", "grafana", "node-exporter", "cadvisor")
GRAFANA_SECRETS = ("GRAFANA_ADMIN_PASSWORD", "GRAFANA_SECRET_KEY")


@pytest.fixture(scope="module")
def vmc() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_monitoring_config", ROOT / "scripts" / "verify_monitoring_config.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_with_cadvisor() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return loaded


# ------------------------------------------------------------------ nothing is published
def test_only_caddy_publishes_host_ports(compose: dict[str, Any]) -> None:
    assert {n: s["ports"] for n, s in compose["services"].items() if s.get("ports")} == {
        "caddy": ["80:80", "443:443"]
    }


@pytest.mark.parametrize("name", [*MONITORING, "postgres", "redis", "app", "migrate"])
def test_no_backend_or_monitoring_service_has_a_host_port(
    compose: dict[str, Any], name: str
) -> None:
    assert not compose["services"][name].get("ports"), name


def test_the_metrics_port_is_not_even_exposed_on_the_web_network(compose: dict[str, Any]) -> None:
    app = compose["services"]["app"]
    assert app["expose"] == ["8000"] and "9464" not in str(app.get("ports", ""))
    assert app["environment"]["TD_METRICS_BIND"] == "172.29.20.10"
    net = compose["networks"]["mon_scrape"]
    subnet = ipaddress.ip_network(net["ipam"]["config"][0]["subnet"])
    assert (
        ipaddress.ip_address(app["networks"]["mon_scrape"]["ipv4_address"])
        in subnet
        == ipaddress.ip_network("172.29.20.0/24")
    )


def test_prometheus_and_grafana_only_talk_over_internal_networks(compose: dict[str, Any]) -> None:
    services, networks = compose["services"], compose["networks"]
    for name in ("mon_scrape", "mon_query", "edge_grafana"):
        assert networks[name]["internal"] is True
    assert set(services["prometheus"]["networks"]) == {"mon_scrape", "mon_query"}
    assert set(services["grafana"]["networks"]) == {"edge_grafana", "mon_query"}
    assert set(services["node-exporter"]["networks"]) == {"mon_scrape"} and set(
        services["cadvisor"]["networks"]
    ) == {"mon_scrape"}
    assert not set(services["prometheus"]["networks"]) & {
        "edge_public",
        "edge_app",
        "edge_grafana",
        "backend",
    }
    assert not set(services["app"]["networks"]) & {"edge_grafana", "mon_query"}
    assert not set(services["grafana"]["networks"]) & {
        "edge_app",
        "backend",
        "mon_scrape",
        "edge_public",
    }
    assert "edge_public" not in services["grafana"]["networks"]  # Grafana has no outbound route


def test_caddy_routes_the_two_hostnames_and_nothing_else(caddyfile: str) -> None:
    assert "reverse_proxy app:8000" in caddyfile and "reverse_proxy grafana:3000" in caddyfile
    assert caddyfile.count("reverse_proxy") == 2
    for target in (
        "prometheus",
        "node-exporter",
        "cadvisor",
        "9090",
        "9100",
        "9464",
        "8080",
        "postgres",
        "redis",
    ):
        assert f"reverse_proxy {target}" not in caddyfile and f":{target}" not in caddyfile.replace(
            "grafana:3000", ""
        ).replace("app:8000", "")
    assert "{$TD_APP_HOSTNAME}" in caddyfile and "{$TD_GRAFANA_HOSTNAME}" in caddyfile


def test_caddy_answers_404_for_metrics_on_both_hosts_and_hides_grafana_health(
    caddyfile: str,
) -> None:
    app_block, grafana_block = caddyfile.split("{$TD_APP_HOSTNAME} {")[1].split(
        "{$TD_GRAFANA_HOSTNAME} {"
    )
    assert "/metrics*" in app_block and "/healthz*" in app_block and "/docs*" in app_block
    assert "/metrics*" in grafana_block and "/api/health*" in grafana_block
    assert grafana_block.count("respond 404") == 1


# ------------------------------------------------------------------ Prometheus
def test_prometheus_retention_and_read_only_flags(compose: dict[str, Any]) -> None:
    command = compose["services"]["prometheus"]["command"]
    assert (
        "--storage.tsdb.retention.time=30d" in command
        and "--storage.tsdb.retention.size=15GB" in command
    )
    assert "--no-web.enable-lifecycle" in command and "--no-web.enable-admin-api" in command
    assert not [
        c
        for c in command
        if c
        in (
            "--web.enable-lifecycle",
            "--web.enable-admin-api",
            "--web.enable-remote-write-receiver",
        )
    ]


def test_prometheus_scrapes_and_evaluates_every_15_seconds_and_has_no_alert_transport() -> None:
    cfg = yaml.safe_load((MON / "prometheus.yml").read_text())
    assert (
        cfg["global"]["scrape_interval"] == "15s" and cfg["global"]["evaluation_interval"] == "15s"
    )
    assert "alerting" not in cfg and "remote_write" not in cfg and "remote_read" not in cfg
    targets = {
        t for job in cfg["scrape_configs"] for s in job["static_configs"] for t in s["targets"]
    }
    assert targets == {"localhost:9090", "app:9464", "node-exporter:9100", "cadvisor:8080"}


def _strings(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in [str(k), *_strings(v)]]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return [str(node)]


def test_rules_cannot_reach_controls_exchanges_or_delivery_channels() -> None:
    """Parsed rule content (keys, labels, annotations, expressions), not the comments."""
    for name in ("alert_rules.yml", "recording_rules.yml"):
        text = " ".join(_strings(yaml.safe_load((MON / name).read_text()))).lower()
        for word in (
            "webhook",
            "slack",
            "pagerduty",
            "smtp",
            "email",
            "http://",
            "https://",
            "coinbase",
            "kill_switch",
            "pause",
            "resume",
            "exec",
            "curl",
            "receiver",
            "route",
        ):
            assert word not in text, (name, word)


def test_the_rule_files_state_that_nothing_is_delivered() -> None:
    header = (MON / "alert_rules.yml").read_text().split("groups:")[0].lower()
    assert "no alertmanager" in header and "displayed" in header


def test_cadvisor_is_optional_and_never_gets_the_docker_socket(compose: dict[str, Any]) -> None:
    cadvisor = compose["services"]["cadvisor"]
    assert cadvisor["profiles"] == ["cadvisor"] and not cadvisor.get("privileged")
    for volume in cadvisor["volumes"]:
        assert (
            volume.endswith(":ro")
            and "docker" not in volume
            and volume.split(":")[0] not in ("/var/run", "/run")
        )
    assert (
        "--docker_only=false" in cadvisor["command"]
        and "--store_container_labels=false" in cadvisor["command"]
    )


def test_monitoring_containers_are_locked_down(compose: dict[str, Any]) -> None:
    for name in MONITORING:
        svc = compose["services"][name]
        assert (
            svc["cap_drop"] == ["ALL"]
            and "no-new-privileges:true" in svc["security_opt"]
            and svc["read_only"] is True
        )
        assert not svc.get("privileged") and "network_mode" not in svc and "pid" not in svc
        assert svc["mem_limit"] and svc["pids_limit"] and svc["healthcheck"]
        assert ":latest" not in svc["image"]
    for name in ("prometheus", "node-exporter"):
        assert compose["services"][name]["user"] == "65534:65534"
    assert compose["services"]["grafana"]["user"] == "472"
    host = [v for v in compose["services"]["node-exporter"]["volumes"] if v.startswith("/")]
    assert host and all(v.endswith(":ro") for v in host)


# ------------------------------------------------------------------ Grafana
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GF_AUTH_ANONYMOUS_ENABLED", "false"),
        ("GF_USERS_ALLOW_SIGN_UP", "false"),
        ("GF_USERS_ALLOW_ORG_CREATE", "false"),
        ("GF_SECURITY_COOKIE_SECURE", "true"),
        ("GF_SECURITY_COOKIE_SAMESITE", "strict"),
        ("GF_SECURITY_ALLOW_EMBEDDING", "false"),
        ("GF_PUBLIC_DASHBOARDS_ENABLED", "false"),
        ("GF_SNAPSHOTS_EXTERNAL_ENABLED", "false"),
        ("GF_ALERTING_ENABLED", "false"),
        ("GF_UNIFIED_ALERTING_ENABLED", "false"),
        ("GF_METRICS_ENABLED", "false"),
        ("GF_PLUGINS_PLUGIN_ADMIN_ENABLED", "false"),
        ("GF_ANALYTICS_REPORTING_ENABLED", "false"),
        ("GF_ANALYTICS_CHECK_FOR_UPDATES", "false"),
        ("GF_AUTH_DISABLE_LOGIN_FORM", "false"),
    ],
)
def test_grafana_hardening(compose: dict[str, Any], key: str, value: str) -> None:
    assert compose["services"]["grafana"]["environment"][key] == value


def test_grafana_has_its_own_admin_name_and_no_default_credentials(compose: dict[str, Any]) -> None:
    env = compose["services"]["grafana"]["environment"]
    assert env["GF_SECURITY_ADMIN_USER"] not in ("admin", "grafana", "")
    assert not any(
        str(v).lower() in ("admin", "grafana", "password", "sw2ycwtib9zpoohopsmm")
        for v in env.values()
    )
    assert (
        env["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")
        and ":-" not in env["GF_SECURITY_ADMIN_PASSWORD"]
    )
    assert (
        env["GF_SECURITY_SECRET_KEY"].startswith("${GRAFANA_SECRET_KEY:?")
        and ":-" not in env["GF_SECURITY_SECRET_KEY"]
    )


def test_grafana_root_url_is_https_on_its_own_hostname(compose: dict[str, Any]) -> None:
    env = compose["services"]["grafana"]["environment"]
    assert env["GF_SERVER_ROOT_URL"] == "https://${TD_GRAFANA_HOSTNAME:?set in .env}/" and env[
        "GF_SERVER_DOMAIN"
    ].startswith("${TD_GRAFANA_HOSTNAME")


# ------------------------------------------------------------------ Grafana secrets in production
@pytest.mark.parametrize("name", GRAFANA_SECRETS)
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "admin",
        "grafana",
        "password",
        "changeme",
        "SW2YcwTIb9zpOOhoPsMm",
        "CHANGE_ME_generate_with_bootstrap",
        "short",
        "x" * 31,
    ],
)
def test_production_validation_rejects_missing_default_or_placeholder_grafana_secrets(
    verify: ModuleType, vmc: ModuleType, name: str, bad: str
) -> None:
    import secrets

    env = {
        n: secrets.token_urlsafe(48)
        for n in (
            *GRAFANA_SECRETS,
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "TD_DB_APP_PASSWORD",
            "TD_DB_CTL_PASSWORD",
        )
    }
    env.update(
        {
            "TD_ENVIRONMENT": "production",
            "TD_SECRET_KEY": secrets.token_urlsafe(48),
            "TD_APP_HOSTNAME": "app.test-host.net",
            "TD_GRAFANA_HOSTNAME": "grafana.test-host.net",
        }
    )
    assert vmc.check_env(env, example=False) == [] and verify.check_env(env, example=False) == []
    env[name] = bad
    assert vmc.check_env(env, example=False), f"monitoring verifier accepted {name}={bad!r}"
    assert verify.check_env(env, example=False), f"security verifier accepted {name}={bad!r}"


def test_a_missing_grafana_secret_is_reported(vmc: ModuleType) -> None:
    assert any(
        "GRAFANA_ADMIN_PASSWORD is missing" in p
        for p in vmc.check_env({"GRAFANA_SECRET_KEY": "k" * 40}, example=False)
    )


def test_well_known_defaults_are_refused_even_in_example_mode(vmc: ModuleType) -> None:
    assert vmc.check_env(
        {"GRAFANA_ADMIN_PASSWORD": "admin", "GRAFANA_SECRET_KEY": "SW2YcwTIb9zpOOhoPsMm"},
        example=True,
    )
    assert (
        vmc.check_env(
            {"GRAFANA_ADMIN_PASSWORD": "CHANGE_ME_x", "GRAFANA_SECRET_KEY": "CHANGE_ME_y"},
            example=True,
        )
        == []
    )


def test_env_example_carries_placeholders_for_both_grafana_secrets() -> None:
    lines = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert all(lines[n].startswith("CHANGE_ME") for n in GRAFANA_SECRETS)


def test_the_secrets_the_production_validator_requires_include_the_grafana_ones(
    verify: ModuleType,
) -> None:
    assert set(GRAFANA_SECRETS) <= set(verify.EXTRA_SECRETS)


# ------------------------------------------------------------ metrics listener production rules
@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "8.8.8.8", "203.0.113.5", "2001:db8::1"])
def test_production_refuses_a_wildcard_or_public_metrics_bind(
    prod_env: dict[str, str], bind: str
) -> None:
    with pytest.raises(ConfigError, match="monitoring.bind_address"):
        load_settings({**prod_env, "TD_METRICS_BIND": bind})


@pytest.mark.parametrize("bind", ["172.29.20.10", "127.0.0.1", "10.1.2.3"])
def test_production_accepts_a_specific_private_metrics_bind(
    prod_env: dict[str, str], bind: str
) -> None:
    assert load_settings({**prod_env, "TD_METRICS_BIND": bind}).monitoring.bind_address == bind


def test_the_default_bind_is_loopback_so_a_missing_setting_is_safe() -> None:
    assert load_settings({"TD_ENVIRONMENT": "test"}).monitoring.bind_address == "127.0.0.1"


@pytest.mark.parametrize(
    "scrapers", [["0.0.0.0/0"], ["10.0.0.0/8"], ["8.8.8.0/24"], ["172.29.0.0/15"], ["::/0"]]
)
def test_production_refuses_broad_or_public_scraper_networks(
    prod_env: dict[str, str], config_copy: Path, scrapers: list[str]
) -> None:
    base = yaml.safe_load((config_copy / "base.yaml").read_text())
    base["monitoring"] = {"allowed_scrapers": scrapers}
    (config_copy / "base.yaml").write_text(yaml.safe_dump(base))
    with pytest.raises(ConfigError, match="allowed_scrapers"):
        load_settings({**prod_env, "TD_CONFIG_DIR": str(config_copy)})


def test_the_default_scrapers_are_the_monitoring_network_and_loopback_only() -> None:
    assert load_settings({"TD_ENVIRONMENT": "test"}).monitoring.allowed_scrapers == (
        "127.0.0.1/32",
        "172.29.20.0/24",
    )
    assert not monitoring_problems(load_settings({"TD_ENVIRONMENT": "test"}).monitoring)


def test_invalid_monitoring_values_are_rejected(
    test_env: dict[str, str], config_copy: Path
) -> None:
    for change in (
        {"bind_address": "not-an-ip"},
        {"allowed_scrapers": ["nope"]},
        {"port": 80},
        {"cache_seconds": 0},
        {"unknown": 1},
    ):
        base = yaml.safe_load((config_copy / "base.yaml").read_text())
        base["monitoring"] = change
        (config_copy / "base.yaml").write_text(yaml.safe_dump(base))
        with pytest.raises(ConfigError):
            load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})


# ------------------------------------------------------------------ the verifier bites
def mutated(change: Any) -> dict[str, Any]:
    doc = copy.deepcopy(compose_with_cadvisor())
    change(doc)
    return doc


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (
            lambda d: d["services"]["prometheus"].update(ports=["9090:9090"]),
            "must not publish host ports",
        ),
        (
            lambda d: d["services"]["grafana"].update(ports=["3000:3000"]),
            "must not publish host ports",
        ),
        (
            lambda d: d["services"]["node-exporter"].update(ports=["9100:9100"]),
            "must not publish host ports",
        ),
        (
            lambda d: d["services"]["cadvisor"].update(ports=["8080:8080"]),
            "must not publish host ports",
        ),
        (
            lambda d: d["services"]["grafana"]["environment"].update(
                GF_AUTH_ANONYMOUS_ENABLED="true"
            ),
            "GF_AUTH_ANONYMOUS_ENABLED",
        ),
        (
            lambda d: d["services"]["grafana"]["environment"].update(GF_USERS_ALLOW_SIGN_UP="true"),
            "GF_USERS_ALLOW_SIGN_UP",
        ),
        (
            lambda d: d["services"]["grafana"]["environment"].update(
                GF_SECURITY_COOKIE_SECURE="false"
            ),
            "GF_SECURITY_COOKIE_SECURE",
        ),
        (
            lambda d: d["services"]["grafana"]["environment"].update(
                GF_UNIFIED_ALERTING_ENABLED="true"
            ),
            "GF_UNIFIED_ALERTING_ENABLED",
        ),
        (
            lambda d: d["services"]["grafana"]["environment"].update(
                GF_SECURITY_ADMIN_PASSWORD="admin"
            ),
            "required secret variable",
        ),
        (
            lambda d: d["services"]["grafana"]["environment"].update(
                GF_SECURITY_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"
            ),
            "required secret variable",
        ),
        (
            lambda d: d["services"]["grafana"]["environment"].update(
                GF_SECURITY_SECRET_KEY="SW2YcwTIb9zpOOhoPsMm"
            ),
            "required secret variable",
        ),
        (
            lambda d: d["services"]["prometheus"]["command"].__setitem__(
                2, "--storage.tsdb.retention.time=90d"
            ),
            "retention time",
        ),
        (
            lambda d: d["services"]["prometheus"]["command"].remove(
                "--storage.tsdb.retention.size=15GB"
            ),
            "retention size",
        ),
        (
            lambda d: d["services"]["prometheus"]["command"].append("--web.enable-admin-api"),
            "must not enable",
        ),
        (
            lambda d: d["services"]["prometheus"]["command"].remove("--no-web.enable-lifecycle"),
            "explicitly disable",
        ),
        (
            lambda d: d["services"]["cadvisor"]["volumes"].append(
                "/var/run/docker.sock:/var/run/docker.sock:ro"
            ),
            "Docker socket",
        ),
        (
            lambda d: d["services"]["cadvisor"]["volumes"].append("/var/run:/var/run:ro"),
            "Docker socket",
        ),
        (
            lambda d: d["services"]["cadvisor"]["volumes"].append(
                "/var/lib/docker:/var/lib/docker:ro"
            ),
            "Docker socket or Docker data",
        ),
        (lambda d: d["services"]["node-exporter"]["volumes"].append("/etc:/host/etc"), "read-only"),
        (
            lambda d: d["services"]["node-exporter"].update(pid="host"),
            "privileged or share host namespaces",
        ),
        (lambda d: d["services"]["cadvisor"].update(privileged=True), "privileged"),
        (lambda d: d["services"]["grafana"].update(image="grafana/grafana-oss:latest"), "pinned"),
        (lambda d: d["services"]["prometheus"].update(profiles=["monitoring"]), "run by default"),
        (lambda d: d["services"]["cadvisor"].pop("profiles"), "optional"),
        (
            lambda d: d["services"]["grafana"].update(
                networks=["edge_grafana", "mon_query", "edge_app"]
            ),
            "networks must be exactly",
        ),
        (lambda d: d["services"]["app"]["networks"].update(mon_query={}), "share no network"),
        (lambda d: d["networks"]["mon_scrape"].pop("internal"), "must be internal"),
        (lambda d: d["services"]["grafana"].pop("healthcheck"), "health check"),
    ],
)
def test_the_verifier_rejects_unsafe_compose_changes(
    vmc: ModuleType, change: Any, fragment: str
) -> None:
    assert any(fragment in p for p in vmc.check_compose(mutated(change))), fragment


def test_the_verifier_accepts_the_real_compose_file(vmc: ModuleType) -> None:
    assert vmc.check_compose(compose_with_cadvisor()) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda c: c["global"].update(scrape_interval="60s"), "scrape_interval"),
        (lambda c: c["global"].update(evaluation_interval="1m"), "evaluation_interval"),
        (
            lambda c: c.update(
                alerting={"alertmanagers": [{"static_configs": [{"targets": ["am:9093"]}]}]}
            ),
            "alertmanagers",
        ),
        (
            lambda c: c.update(remote_write=[{"url": "https://metrics.example/write"}]),
            "remote_write",
        ),
        (lambda c: c.update(remote_read=[{"url": "https://metrics.example/read"}]), "remote_read"),
        (
            lambda c: c["scrape_configs"].append(
                {"job_name": "evil", "static_configs": [{"targets": ["evil.example:80"]}]}
            ),
            "scrape jobs",
        ),
        (
            lambda c: c["scrape_configs"][1]["static_configs"][0]["targets"].append(
                "169.254.169.254:80"
            ),
            "approved internal target",
        ),
        (
            lambda c: c["scrape_configs"][1].update(basic_auth={"username": "x", "password": "y"}),
            "basic_auth",
        ),
        (lambda c: c["scrape_configs"][1].update(proxy_url="http://proxy"), "proxy_url"),
        (lambda c: c.update(rule_files=["/etc/prometheus/extra.yml"]), "rule_files"),
    ],
)
def test_the_verifier_rejects_unsafe_prometheus_changes(
    vmc: ModuleType, change: Any, fragment: str
) -> None:
    cfg = yaml.safe_load((MON / "prometheus.yml").read_text())
    assert vmc.check_prometheus_config(cfg) == []
    change(cfg)
    assert any(fragment in p for p in vmc.check_prometheus_config(cfg)), fragment


def _rules() -> tuple[dict[str, Any], dict[str, Any]]:
    return yaml.safe_load((MON / "alert_rules.yml").read_text()), yaml.safe_load(
        (MON / "recording_rules.yml").read_text()
    )


def _first_rule(alert: dict[str, Any]) -> dict[str, Any]:
    rule: dict[str, Any] = alert["groups"][0]["rules"][0]
    return rule


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda a, r: _first_rule(a).update(webhook="https://hooks.example"), "unexpected keys"),
        (lambda a, r: _first_rule(a)["labels"].update(action="pause_bot"), "labels must be within"),
        (lambda a, r: _first_rule(a)["labels"].update(severity="page"), "severity"),
        (lambda a, r: _first_rule(a)["labels"].update(component="bot"), "component"),
        (
            lambda a, r: _first_rule(a)["annotations"].update(
                description="POST to https://x.example/pause"
            ),
            "delivery channel",
        ),
        (
            lambda a, r: _first_rule(a)["annotations"].update(summary="notify slack"),
            "delivery channel",
        ),
        (
            lambda a, r: _first_rule(a)["annotations"].update(description="call coinbase"),
            "delivery channel",
        ),
        (
            lambda a, r: _first_rule(a)["annotations"].update(runbook="https://wiki.example/x"),
            "runbook",
        ),
        (
            lambda a, r: _first_rule(a)["annotations"].update(extra="x"),
            "annotations must be exactly",
        ),
        (
            lambda a, r: _first_rule(a).update(expr="tradingdots_bot_open_orders > 5"),
            "unknown metric",
        ),
        (lambda a, r: _first_rule(a).update(expr='up{user="x"} == 0'), "sensitive label"),
        (lambda a, r: _first_rule(a).update(expr="sum("), "invalid PromQL"),
        (lambda a, r: _first_rule(a).update(alert="target_down"), "CamelCase"),
        (lambda a, r: a["groups"][0]["rules"].append(dict(_first_rule(a))), "duplicate"),
        (
            lambda a, r: r["groups"][0]["rules"][0].update(record="bad_name"),
            "level:metric:operation",
        ),
        (
            lambda a, r: a["groups"][0]["rules"].append({"record": "x:y:z", "expr": "up"}),
            "only contain alert rules",
        ),
    ],
)
def test_the_verifier_rejects_unsafe_rule_changes(
    vmc: ModuleType, change: Any, fragment: str
) -> None:
    alert, recording = _rules()
    assert vmc.check_rules(alert, recording)[0] == []
    change(alert, recording)
    assert any(fragment in p for p in vmc.check_rules(alert, recording)[0]), fragment


@pytest.mark.parametrize(
    "text",
    [
        "reverse_proxy prometheus:9090",
        "reverse_proxy node-exporter:9100",
        "reverse_proxy cadvisor:8080",
        "reverse_proxy app:9464",
    ],
)
def test_the_verifier_rejects_caddy_proxying_monitoring_targets(
    vmc: ModuleType, caddyfile: str, text: str
) -> None:
    assert vmc.check_caddy(caddyfile) == []
    assert any(
        "never proxy" in p
        for p in vmc.check_caddy(
            caddyfile.replace(
                "reverse_proxy grafana:3000", text + "\n\t\treverse_proxy grafana:3000"
            )
        )
    )


def test_the_verifier_rejects_caddy_that_does_not_hide_metrics_or_health(
    vmc: ModuleType, caddyfile: str
) -> None:
    assert any(
        "/metrics" in p for p in vmc.check_caddy(caddyfile.replace("/metrics*", "/nothing*"))
    )
    assert any(
        "/api/health" in p for p in vmc.check_caddy(caddyfile.replace("/api/health*", "/nothing*"))
    )
    assert any(
        "grafana:3000" in p
        for p in vmc.check_caddy(caddyfile.replace("reverse_proxy grafana:3000", "respond 503"))
    )
