"""Shared fixtures. Secrets used in tests are generated at runtime; none are committed."""

from __future__ import annotations

import importlib.util
import secrets
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent

# Phase 1.0 failing skeletons for later phases are parked here and not collected.
collect_ignore_glob = ["pending/*"]


@pytest.fixture
def test_env() -> dict[str, str]:
    return {"TD_ENVIRONMENT": "test"}


@pytest.fixture
def prod_env() -> dict[str, str]:
    return {
        "TD_ENVIRONMENT": "production",
        "TD_SECRET_KEY": secrets.token_urlsafe(48),
        "TD_APP_HOSTNAME": "app.test-host.net",
        "TD_GRAFANA_HOSTNAME": "grafana.test-host.net",
    }


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target


@pytest.fixture
def settings(test_env: dict[str, str]) -> Settings:
    return load_settings(test_env)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture(scope="session")
def verify() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_security_config", ROOT / "scripts" / "verify_security_config.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return loaded


@pytest.fixture
def caddyfile() -> str:
    return (ROOT / "infra" / "caddy" / "Caddyfile").read_text()
