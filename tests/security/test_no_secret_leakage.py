from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, load_settings
from app.logging import RedactingFormatter, redact, setup_logging

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("connect postgresql://td:hunter2pass@db:5432/x", "hunter2pass"),
        ("login password=hunter2pass ok", "hunter2pass"),
        ("Authorization: Bearer abcdef1234567890token", "abcdef1234567890token"),
        ("cookie=sessionvalue123456", "sessionvalue123456"),
        ("api_key: 'sk-live-abcdef'", "sk-live-abcdef"),
        ("client 203.0.113.77 connected", "203.0.113.77"),
        ("peer 2001:db8:85a3:8d3:1319:8a2e:370:7348", "2001:db8:85a3:8d3:1319:8a2e:370:7348"),
    ],
)
def test_redaction(raw: str, secret: str) -> None:
    assert secret not in redact(raw)


def test_redaction_keeps_bind_and_loopback_addresses_readable() -> None:
    assert redact("on 0.0.0.0:8000 and 127.0.0.1") == "on 0.0.0.0:8000 and 127.0.0.1"


def test_redaction_keeps_timestamps_readable() -> None:
    assert redact("at 10:37:43 UTC") == "at 10:37:43 UTC"


def test_formatter_redacts_exception_text() -> None:
    try:
        raise RuntimeError("password=hunter2pass")  # noqa: TRY301
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "app", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    assert "hunter2pass" not in RedactingFormatter("%(message)s").format(record)


def test_configured_logger_redacts_output(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO")
    logging.getLogger("app").info("token=%s from %s", "canarytoken12345", "198.51.100.23")
    out = capsys.readouterr().out
    assert "canarytoken12345" not in out
    assert "198.51.100.23" not in out
    assert "token=***" in out


def test_config_errors_and_repr_do_not_contain_secret_values(prod_env: dict[str, str]) -> None:
    bad = "CHANGE_ME_" + secrets.token_hex(20)
    with pytest.raises(ConfigError) as info:
        load_settings({**prod_env, "TD_SECRET_KEY": bad})
    assert bad not in str(info.value)
    good = load_settings(prod_env)
    assert prod_env["TD_SECRET_KEY"] not in repr(good)
    assert prod_env["TD_SECRET_KEY"] not in good.model_dump_json()


def test_health_and_error_pages_do_not_leak(
    monkeypatch: pytest.MonkeyPatch, admin_client: TestClient, client: TestClient, secret_key: str
) -> None:
    canary = "canary-" + secrets.token_hex(8)
    assert secret_key not in client.get("/healthz").text

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"password={canary}")

    monkeypatch.setattr("app.api.dashboard.build_dashboard", boom)
    response = admin_client.get("/")
    assert response.status_code == 500
    assert (
        canary not in response.text
        and "Traceback" not in response.text
        and "RuntimeError" not in response.text
    )
    assert "Something went wrong." in response.text


def test_database_outages_show_a_generic_503(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    from app.storage.database import StorageUnavailable

    def down(*args: object, **kwargs: object) -> None:
        raise StorageUnavailable(
            "connection to server at 10.1.2.3 failed: password authentication failed"
        )

    monkeypatch.setattr("app.auth.session.AuthService.login", down)
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    response = client.post("/login", data={"csrf_token": token, "username": "a", "password": "b"})
    assert response.status_code == 503
    assert "temporarily unavailable" in response.text
    assert "10.1.2.3" not in response.text and "password authentication" not in response.text


def test_secrets_never_appear_in_the_application_log(
    admin_client: TestClient, admin: object, capsys: pytest.CaptureFixture[str], secret_key: str
) -> None:
    setup_logging("DEBUG")
    admin_client.get("/security")
    admin_client.get("/audit")
    logged = capsys.readouterr()
    assert secret_key not in logged.out + logged.err
    assert (admin_client.cookies.get("__Host-td_session") or "-") not in logged.out + logged.err


def test_env_example_contains_placeholders_only() -> None:
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    for key in (
        "TD_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "GRAFANA_ADMIN_PASSWORD",
        "TD_DB_APP_PASSWORD",
        "TD_DB_CTL_PASSWORD",
    ):
        assert values[key].startswith("CHANGE_ME"), key


def test_repository_contains_no_key_material_or_real_env() -> None:
    assert not (ROOT / ".env").exists()
    pattern = re.compile(r"BEGIN (?:EC |RSA |OPENSSH )?PRIVATE KEY|organizations/[^/\s]+/apiKeys")
    skip = {".git", ".venv", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__"}
    for path in ROOT.rglob("*"):
        if path.is_dir() or skip & set(path.parts) or path.name in {"uv.lock", "htmx.min.js"}:
            continue
        if path.suffix in {".png", ".ico", ".gz"}:
            continue
        assert not pattern.search(path.read_text(errors="ignore")), path
