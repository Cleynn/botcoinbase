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


def test_only_caddy_publishes_ports_and_only_80_443(compose: dict[str, Any]) -> None:
    published = {n: s["ports"] for n, s in compose["services"].items() if s.get("ports")}
    assert published == {"caddy": ["80:80", "443:443"]}


@pytest.mark.parametrize("service", INTERNAL)
def test_publishing_any_internal_service_fails(
    verify: ModuleType, compose: dict[str, Any], service: str
) -> None:
    mutated = copy.deepcopy(compose)
    mutated["services"].setdefault(service, {"image": "x:1", "cap_drop": ["ALL"]})
    mutated["services"][service]["ports"] = ["9000:9000"]
    assert any("publishes host ports" in p for p in verify.check_compose(mutated))


@pytest.mark.parametrize(
    "ports", [["80:80", "443:443", "8080:8080"], ["80:80", "443:443/udp"], ["80:80"]]
)
def test_caddy_must_publish_exactly_tcp_80_443(
    verify: ModuleType, compose: dict[str, Any], ports: list[str]
) -> None:
    mutated = copy.deepcopy(compose)
    mutated["services"]["caddy"]["ports"] = ports
    assert any("exactly TCP 80 and 443" in p for p in verify.check_compose(mutated))


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
        ("TD_APP_HOSTNAME", "not a host"),
        ("TD_GRAFANA_HOSTNAME", ""),
    ]:
        assert verify.check_env({**_good_env(), key: value}, example=False), key


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
