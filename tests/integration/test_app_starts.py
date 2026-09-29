"""Application-level behaviour carried over from Phase 1, updated for authenticated access."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import ConfigError, Settings, load_settings
from app.storage.database import SchemaError, Storage
from tests.conftest import Account, FakeClock, PgTarget

ROOT = Path(__file__).resolve().parents[2]
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


def test_healthz_is_generic_and_public(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


def test_login_page_carries_the_safety_banner(client: TestClient) -> None:
    body = client.get("/login").text
    assert "MODE: BACKTEST" in body and "LIVE TRADING: BLOCKED" in body


def test_paper_mode_banner_still_blocks_live(
    settings: Settings,
    storage: Storage,
    clock: FakeClock,
    admin: Account,
    login: Callable[..., Any],
) -> None:
    paper = settings.model_copy(update={"mode": "PAPER"})
    client = TestClient(
        create_app(paper, storage=storage, clock=clock),
        base_url="https://testserver",
        client=("10.0.0.7", 1),
        headers={"origin": "https://testserver"},
    )
    login(client, admin.username, admin.password)
    body = client.get("/").text
    assert "MODE: PAPER" in body and "LIVE TRADING: BLOCKED" in body


def test_security_headers_on_public_and_authenticated_pages(
    client: TestClient, admin_client: TestClient
) -> None:
    for c, path in (
        (client, "/login"),
        (admin_client, "/"),
        (admin_client, "/security"),
        (admin_client, "/audit"),
    ):
        headers = c.get(path).headers
        assert "script-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "form-action 'self'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert headers["x-frame-options"] == "DENY"
        assert headers["cache-control"] == "no-store" and "cookie" in headers["vary"].lower()
        assert headers["x-request-id"]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/metrics", "/nope"])
def test_introspection_and_unknown_routes_are_404_with_generic_page(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "Page not found." in response.text
    assert path not in response.text


def test_unknown_host_header_is_rejected(client: TestClient) -> None:
    assert client.get("/login", headers={"host": "evil.example.com"}).status_code == 400


def test_static_files_are_public_and_traversal_is_blocked(client: TestClient) -> None:
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/htmx.min.js").status_code == 200
    assert client.get("/static/%2e%2e/%2e%2e/config/base.yaml").status_code == 404
    assert client.get("/static/../config/base.yaml").status_code == 404


def test_vendored_htmx_matches_pinned_checksum() -> None:
    data = (ROOT / "app/web/static/js/htmx.min.js").read_bytes()
    assert hashlib.sha256(data).hexdigest() == HTMX_SHA256


def test_no_frontend_build_chain_or_unsafe_template_constructs() -> None:
    for name in (
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "vite.config.js",
    ):
        assert not (ROOT / name).exists()
    assert not (ROOT / "node_modules").exists()
    for template in (ROOT / "app/web/templates").glob("*.html"):
        text = template.read_text()
        assert "|safe" not in text and "Markup" not in text
        assert "hx-on" not in text and "hx-swap-oob" not in text and "<script>" not in text


def test_static_javascript_never_uses_browser_storage() -> None:
    for script in (ROOT / "app/web/static/js").glob("*.js"):
        if script.name == "htmx.min.js":
            continue  # vendored, checksum-pinned; htmx history caching is disabled via config
        text = script.read_text()
        assert not any(
            word in text
            for word in ("localStorage", "sessionStorage", "indexedDB", "document.cookie")
        )


def test_htmx_is_configured_without_eval_or_history_cache() -> None:
    base = (ROOT / "app/web/templates/base.html").read_text()
    for setting in ('"allowEval":false', '"selfRequestsOnly":true', '"historyCacheSize":0'):
        assert setting in base


def test_app_requires_a_secret_key(settings: Settings, storage: Storage) -> None:
    with pytest.raises(ConfigError, match="TD_SECRET_KEY"):
        create_app(settings.model_copy(update={"secret_key": None}), storage=storage)


def test_app_refuses_an_uninitialised_database(settings: Settings, pg_target: PgTarget) -> None:
    import psycopg

    from app.config import DatabaseSettings

    name = "td_empty_check"
    with psycopg.connect(pg_target.conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    try:
        empty = settings.model_copy(
            update={
                "database": DatabaseSettings(
                    host=pg_target.host,
                    port=pg_target.port,
                    name=name,
                    user=pg_target.user,
                    owner_user=pg_target.user,
                )
            }
        )
        with pytest.raises(SchemaError):
            create_app(empty, storage=Storage(empty.database))
    finally:
        with psycopg.connect(pg_target.conninfo("postgres"), autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    assert load_settings({"TD_ENVIRONMENT": "test"}).environment == "test"


def test_referrer_policy_never_makes_browsers_send_a_null_origin(client: TestClient) -> None:
    """Regression (found in a real browser): `no-referrer` turns same-origin POST Origins into
    "null", so every legitimate form submission would fail the CSRF Origin check."""
    policy = client.get("/login").headers["referrer-policy"]
    assert policy not in {"no-referrer", ""}
    assert policy in {"same-origin", "strict-origin", "origin", "strict-origin-when-cross-origin"}
