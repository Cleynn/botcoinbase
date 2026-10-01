from __future__ import annotations

import copy
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash") or "/bin/bash"
INTERNAL = [
    "app",
    "postgres",
    "redis",
    "prometheus",
    "grafana",
    "worker",
    "node-exporter",
    "cadvisor",
]


def test_real_compose_passes(verify: ModuleType, compose: dict[str, Any]) -> None:
    assert verify.check_compose(compose) == []


def test_only_caddy_publishes_ports_and_only_loopback_8080_444(compose: dict[str, Any]) -> None:
    published = {n: s["ports"] for n, s in compose["services"].items() if s.get("ports")}
    assert published == {"caddy": ["127.0.0.1:8080:80", "127.0.0.1:444:443"]}


@pytest.mark.parametrize("service", INTERNAL)
def test_publishing_any_internal_service_fails(
    verify: ModuleType, compose: dict[str, Any], service: str
) -> None:
    mutated = copy.deepcopy(compose)
    mutated["services"].setdefault(service, {"image": "x:1", "cap_drop": ["ALL"]})
    mutated["services"][service]["ports"] = ["9000:9000"]
    assert any("publishes host ports" in p for p in verify.check_compose(mutated))


@pytest.mark.parametrize(
    "ports",
    [
        ["127.0.0.1:8080:80", "127.0.0.1:444:443", "127.0.0.1:9000:9000"],
        ["127.0.0.1:8080:80", "127.0.0.1:444:443/udp"],
        ["127.0.0.1:8080:80"],
        ["80:80", "443:443"],
    ],
)
def test_caddy_must_publish_exactly_tcp_8080_444(
    verify: ModuleType, compose: dict[str, Any], ports: list[str]
) -> None:
    mutated = copy.deepcopy(compose)
    mutated["services"]["caddy"]["ports"] = ports
    assert any("exactly TCP 8080 and 444" in p for p in verify.check_compose(mutated))


@pytest.mark.parametrize(
    "ports",
    [
        ["8080:80", "444:443"],
        ["0.0.0.0:8080:80", "127.0.0.1:444:443"],
        [
            {"target": 80, "published": 8080, "host_ip": "127.0.0.1"},
            {"target": 443, "published": 444},
        ],
    ],
)
def test_caddy_must_publish_on_loopback_only(
    verify: ModuleType, compose: dict[str, Any], ports: list[Any]
) -> None:
    mutated = copy.deepcopy(compose)
    mutated["services"]["caddy"]["ports"] = ports
    assert verify.check_compose(mutated) == ["caddy must publish on 127.0.0.1 only"]


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("network_mode", "host", "host networking"),
        ("privileged", True, "privileged"),
        ("volumes", ["/var/run/docker.sock:/var/run/docker.sock"], "Docker socket"),
        ("image", "postgres:latest", "unpinned"),
        ("cap_drop", [], "capabilities"),
        ("user", "root", "non-root"),
    ],
)
def test_unsafe_container_settings_fail(
    verify: ModuleType, compose: dict[str, Any], key: str, value: object, fragment: str
) -> None:
    mutated = copy.deepcopy(compose)
    target = "app" if key == "user" else "postgres"
    mutated["services"][target][key] = value
    assert any(fragment in p for p in verify.check_compose(mutated))


def test_missing_health_check_or_restart_policy_fails(
    verify: ModuleType, compose: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(compose)
    del mutated["services"]["redis"]["healthcheck"]
    del mutated["services"]["postgres"]["restart"]
    problems = verify.check_compose(mutated)
    assert any("'redis' has no health check" in p for p in problems)
    assert any("'postgres' has no restart policy" in p for p in problems)


def test_internal_services_never_join_a_non_internal_network(
    verify: ModuleType, compose: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(compose)
    mutated["networks"]["backend"] = {}
    assert any("non-internal network" in p for p in verify.check_compose(mutated))
    mutated = copy.deepcopy(compose)
    mutated["services"]["postgres"]["networks"] = ["edge_public"]
    assert any("non-internal network" in p for p in verify.check_compose(mutated))


def test_real_caddyfile_passes_and_regressions_fail(verify: ModuleType, caddyfile: str) -> None:
    assert verify.check_caddyfile(caddyfile) == []
    assert verify.check_caddyfile(caddyfile.replace("admin off", "")) != []
    assert (
        verify.check_caddyfile(caddyfile + "\nx.example.com {\n reverse_proxy prometheus:9090\n}\n")
        != []
    )
    assert verify.check_caddyfile(caddyfile.replace("/metrics*", "/nothing")) != []
    assert verify.check_caddyfile(caddyfile.replace("trusted_proxies_strict", "")) != []
    single_address = "header_up X-Forwarded-For {client_ip}"
    assert verify.check_caddyfile(caddyfile.replace(single_address, "")) != []
    assert (
        verify.check_caddyfile(caddyfile.replace("{$TD_GRAFANA_HOSTNAME}", "grafana.example.com"))
        != []
    )


def _good_env() -> dict[str, str]:
    return {
        "TD_ENVIRONMENT": "production",
        "TD_DEBUG": "false",
        "TD_PROFILE": "backtest",
        "TD_APP_HOSTNAME": "app.test-host.net",
        "TD_GRAFANA_HOSTNAME": "grafana.test-host.net",
        "TD_SECRET_KEY": secrets.token_urlsafe(48),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(48),
        "REDIS_PASSWORD": secrets.token_urlsafe(48),
        "GRAFANA_ADMIN_PASSWORD": secrets.token_urlsafe(48),
        "GRAFANA_SECRET_KEY": secrets.token_urlsafe(48),
        "TD_DB_APP_PASSWORD": secrets.token_urlsafe(48),
        "TD_DB_CTL_PASSWORD": secrets.token_urlsafe(48),
    }


def test_env_validator_accepts_good_and_rejects_bad(verify: ModuleType) -> None:
    assert verify.check_env(_good_env(), example=False) == []
    for key, value in [
        ("TD_DEBUG", "true"),
        ("TD_ENVIRONMENT", "development"),
        ("TD_SECRET_KEY", "CHANGE_ME_" + "a" * 40),
        ("POSTGRES_PASSWORD", "CHANGE_ME"),
        ("REDIS_PASSWORD", ""),
        ("GRAFANA_ADMIN_PASSWORD", "short"),
        ("GRAFANA_SECRET_KEY", "SW2YcwTIb9zpOOhoPsMm"),
        ("GRAFANA_SECRET_KEY", ""),
        ("TD_DB_APP_PASSWORD", "CHANGE_ME"),
        ("TD_DB_APP_PASSWORD", ""),
        ("TD_DB_CTL_PASSWORD", "short"),
        ("TD_APP_HOSTNAME", "not a host"),
        ("TD_GRAFANA_HOSTNAME", ""),
    ]:
        assert verify.check_env({**_good_env(), key: value}, example=False), key


def test_compose_gives_each_service_only_its_own_database_role(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert services["app"]["environment"]["TD_DB_USER"] == "td_app"
    assert services["ctl"]["environment"]["TD_DB_USER"] == "td_ctl"
    assert "ctl" not in [
        n for n, sv in services.items() if not sv.get("profiles")
    ]  # never started by `make up`
    owner = "TD_DB_OWNER_PASSWORD"
    holders = {n for n, sv in services.items() if owner in sv.get("environment", {})}
    assert holders == {"migrate"}  # only the one-shot migration ever sees the owner password
    assert "TD_DB_CTL_PASSWORD" not in services["app"]["environment"]


def test_app_reaches_the_database_only_over_an_internal_network(compose: dict[str, Any]) -> None:
    services, networks = compose["services"], compose["networks"]
    assert "backend" in services["app"]["networks"] and networks["backend"]["internal"] is True
    # edge_app (Caddy), backend (database), mon_scrape (Prometheus); never edge_public or Grafana's
    assert set(services["app"]["networks"]) == {"edge_app", "backend", "mon_scrape"}
    assert "edge_public" not in services["app"]["networks"]  # the app has no outbound route


def test_migrate_is_a_one_shot_gate_for_the_app(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert str(services["migrate"]["restart"]) == "no"
    assert services["app"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


def test_env_example_fails_strict_but_passes_structure_check(verify: ModuleType) -> None:
    env = verify.parse_env_file(ROOT / ".env.example")
    assert verify.check_env(env, example=False) != []
    assert verify.check_env(env, example=True) == []


def test_env_file_permissions_are_enforced(verify: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{k}={v}" for k, v in _good_env().items()) + "\n")
    path.chmod(0o644)
    assert any("chmod 600" in p for p in verify.check_env(_good_env(), example=False, path=path))
    path.chmod(0o600)
    assert verify.check_env(_good_env(), example=False, path=path) == []


@pytest.mark.skipif(shutil.which("docker") is None, reason="bootstrap.sh requires docker on PATH")
def test_bootstrap_generates_valid_private_env_without_printing_secrets(
    verify: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/bootstrap.sh", tmp_path / "scripts/bootstrap.sh")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    result = subprocess.run(  # noqa: S603
        [BASH, str(tmp_path / "scripts/bootstrap.sh")], capture_output=True, text=True, check=True
    )
    env_path = tmp_path / ".env"
    assert oct(env_path.stat().st_mode & 0o777) == oct(0o600)
    env = verify.parse_env_file(env_path)
    assert not any("CHANGE_ME" in value for value in env.values())
    assert verify.check_env(env, example=False, path=env_path) == []
    assert all(v not in result.stdout + result.stderr for k, v in env.items() if "PASSWORD" in k)
    again = subprocess.run([BASH, str(tmp_path / "scripts/bootstrap.sh")], capture_output=True)  # noqa: S603
    assert again.returncode != 0 and os.path.exists(env_path)


# ------------------------------------------------------------------ Phase 4: discovery profile
def test_discovery_services_are_profile_only_and_unpublished(compose: dict[str, Any]) -> None:
    for name in ("egress-proxy", "pairs"):
        svc = compose["services"][name]
        assert svc["profiles"] == ["discovery"] and not svc.get("ports")
        assert svc["read_only"] is True and "ALL" in svc["cap_drop"]
    assert compose["services"]["pairs"]["restart"] == "no"


def test_only_the_egress_proxy_has_a_route_to_the_internet(compose: dict[str, Any]) -> None:
    external = [n for n, net in compose["networks"].items() if not (net or {}).get("internal")]
    assert set(external) == {"edge_public", "egress_ext"}
    for name, svc in compose["services"].items():
        nets = set(svc.get("networks") or [])
        if "egress_ext" in nets:
            assert name == "egress-proxy"
        if "edge_public" in nets:
            assert name == "caddy"
    assert set(compose["services"]["pairs"]["networks"]) == {"backend", "egress_int"}
    assert set(compose["services"]["egress-proxy"]["networks"]) == {"egress_int", "egress_ext"}
    assert "egress_int" not in (compose["services"]["app"]["networks"])


def test_the_runner_uses_the_host_role_and_never_the_web_or_owner_credentials(
    compose: dict[str, Any],
) -> None:
    env = compose["services"]["pairs"]["environment"]
    assert env["TD_DB_USER"] == "td_ctl" and "CTL_PASSWORD" in env["TD_DB_PASSWORD"]
    assert not any("OWNER" in str(v) or "APP_PASSWORD" in str(v) for v in env.values())
    assert env["TD_EGRESS_PROXY"] == "http://egress-proxy:3128"


def test_the_egress_proxy_cannot_be_moved_onto_other_networks_or_run_by_default(
    verify: ModuleType, compose: dict[str, Any]
) -> None:
    assert verify.check_compose(compose) == []
    mutated = copy.deepcopy(compose)
    mutated["services"]["app"]["networks"] = [*mutated["services"]["app"]["networks"], "egress_ext"]
    assert any("non-internal network 'egress_ext'" in p for p in verify.check_compose(mutated))
    mutated = copy.deepcopy(compose)
    mutated["services"]["egress-proxy"]["profiles"] = []
    assert any("discovery profile" in p for p in verify.check_compose(mutated))
    mutated = copy.deepcopy(compose)
    mutated["services"]["postgres"]["networks"] = [
        *mutated["services"]["postgres"]["networks"],
        "egress_int",
    ]
    assert any("must not join egress_int" in p for p in verify.check_compose(mutated))
