from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import load_settings

ROOT = Path(__file__).resolve().parents[2]
HTMX_SHA256 = "d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717"


def test_healthz_is_generic(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_shows_mode_and_blocked_live(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "MODE: BACKTEST" in response.text
    assert "LIVE TRADING: BLOCKED" in response.text
    assert "Not available" in response.text
    assert "Unknown" in response.text


def test_paper_mode_banner_still_blocks_live() -> None:
    settings = load_settings({"TD_ENVIRONMENT": "test", "TD_PROFILE": "paper"})
    body = TestClient(create_app(settings)).get("/").text
    assert "MODE: PAPER" in body
    assert "LIVE TRADING: BLOCKED" in body


def test_dashboard_is_accessible_and_mobile_friendly(client: TestClient) -> None:
    body = client.get("/").text
    assert '<html lang="en">' in body
    assert 'name="viewport"' in body
    assert 'class="skip-link"' in body
    assert "<main" in body
    assert 'aria-label="Main"' in body


def test_works_without_javascript_and_no_external_assets(client: TestClient) -> None:
    body = client.get("/").text
    assert not re.search(r"<script(?![^>]*\ssrc=)", body), "inline scripts are forbidden"
    assert not re.search(r'(?:src|href)="(?:https?:)?//(?!grafana\.)', body)
    assert "<form" not in body  # no interactive control exists in Phase 1


def test_security_headers_present(client: TestClient) -> None:
    headers = client.get("/").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert "permissions-policy" in headers
    assert headers["cache-control"] == "no-store"
    assert "server" not in {k.lower() for k in headers} or "uvicorn" not in headers["server"]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/metrics", "/nope"])
def test_introspection_and_unknown_routes_are_404_with_generic_page(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "Page not found." in response.text
    assert path not in response.text or path == "/"  # path is never echoed


def test_login_is_a_notice_not_a_form(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "not implemented" in response.text
    assert "<form" not in response.text


def test_status_partial_is_a_fragment(client: TestClient) -> None:
    response = client.get("/partials/status")
    assert response.status_code == 200
    assert "UTC" in response.text
    assert "<html" not in response.text


def test_unknown_host_header_is_rejected(client: TestClient) -> None:
    assert client.get("/", headers={"host": "evil.example.com"}).status_code == 400


def test_static_files_served_and_traversal_blocked(client: TestClient) -> None:
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
        assert "hx-on" not in text and "hx-swap-oob" not in text
