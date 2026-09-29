"""Review packages cannot reach an LLM, an exchange, the network, or any bot state."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import ReviewSettings
from app.domain.enums import AuditEventType
from app.review.exporter import SCHEMAS, SCOPE_FILES
from app.review.schema import SCOPES
from tests.security.test_pair_no_exchange_access import APP, imports_of

REVIEW = APP / "review"
NETWORK = (
    "httpx",
    "requests",
    "urllib",
    "socket",
    "http.client",
    "aiohttp",
    "app.adapters",
    "anthropic",
    "openai",
    "websockets",
    "smtplib",
    "subprocess",
    "ftplib",
)


def review_sources() -> dict[Path, str]:
    return {p: p.read_text() for p in sorted(REVIEW.glob("*.py"))}


def test_the_review_package_imports_no_network_llm_or_process_code() -> None:
    for path, source in review_sources().items():
        bad = [m for m in imports_of(source) if m.startswith(NETWORK)]
        assert not bad, (path, bad)


def test_review_code_never_imports_bot_pair_exchange_or_paper_execution_modules() -> None:
    banned = (
        "app.paper",
        "app.backtest",
        "app.strategy",
        "app.market",
        "app.adapters",
        "app.pairs.service",
    )
    for path, source in review_sources().items():
        bad = [m for m in imports_of(source) if m.startswith(banned)]
        assert not bad, (path, bad)


def test_no_llm_or_proposal_or_order_words_in_review_code() -> None:
    for path, source in review_sources().items():
        tree = ast.parse(source)
        names = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr.lower() for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for word in (
            "anthropic",
            "openai",
            "llm_client",
            "import_proposal",
            "create_order",
            "cancel_order",
            "place_order",
        ):
            assert word not in names | attrs, (path, word)
        strings = [
            n.value.lower()
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 300
        ]
        for word in ("api.anthropic", "api.openai", "/api/v3", "/orders", "cb-access"):
            if path.name == "sanitizer.py":
                continue  # its scanner rules name these strings on purpose, to refuse them
            assert not [s for s in strings if word in s], (path, word)


def test_review_sql_only_writes_review_tables() -> None:
    source = (APP / "storage" / "review_repositories.py").read_text()
    writes = re.findall(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([a-z_]+)", source)
    assert writes and set(writes) <= {"review_packages", "review_settings"}, set(writes)
    export_part = source[source.index("class ExportRepository") :]
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER)\b", export_part)


def _export_sql() -> str:
    tree = ast.parse((APP / "storage" / "review_repositories.py").read_text())
    (cls,) = [
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ExportRepository"
    ]
    parts = [
        n.value
        for n in ast.walk(cls)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "SELECT" in n.value
        or (isinstance(n, ast.Constant) and isinstance(n.value, str) and " FROM " in n.value)
    ]
    return " ".join(parts)


def test_exporters_select_no_free_text_identity_or_secret_columns() -> None:
    sql = _export_sql()
    assert "FROM" in sql
    for column in (
        "password",
        "hash",
        "session",
        "cookie",
        "client_tag",
        "request_id",
        "actor_user_id",
        "username",
        "title",
        "body_json",
        "body_md",
        "detail",
        "target_id",
        "user_agent",
        "source_ip",
    ):
        assert not re.search(rf"\b{re.escape(column)}", sql), column


def test_every_scope_has_files_and_every_file_has_a_typed_schema() -> None:
    assert set(SCOPE_FILES) == set(SCOPES)
    assert {p for files in SCOPE_FILES.values() for p in files} == set(SCHEMAS)


def test_every_review_audit_event_is_in_the_catalogue() -> None:
    wanted = {
        "enabled",
        "disabled",
        "requested",
        "generating",
        "ready",
        "failed",
        "corrupt",
        "expired",
        "verified",
        "downloaded",
        "cleanup",
        "denied",
    }
    have = {e.value.split(".", 1)[1] for e in AuditEventType if e.value.startswith("review.")}
    assert have == wanted


def test_no_review_route_writes_bot_or_pair_state() -> None:
    source = (APP / "api" / "review.py").read_text() + (APP / "review" / "service.py").read_text()
    for word in (
        "pairs.simple_action",
        "pairs.confirm",
        "paper.",
        "PairService",
        "apply_transition",
    ):
        assert word not in source, word


def test_the_review_directory_cannot_be_configured_inside_the_web_root() -> None:
    for bad in (
        "/app/web/static/x",
        "/srv/static/x",
        "/var/public/x",
        "relative/x",
        "/a/../b",
        "/app/review",
    ):
        with pytest.raises(ValidationError):
            ReviewSettings(dir=bad)
    assert ReviewSettings().dir == "/review"


def test_compose_gives_the_web_tier_a_read_only_view_of_packages(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert "review_packages:/review:ro" in services["app"]["volumes"]
    assert "review_packages:/review" in services["batch"]["volumes"]
    others = [
        n
        for n, s in services.items()
        if n not in ("app", "batch")
        and any("review_packages" in str(v) for v in s.get("volumes") or [])
    ]
    assert others == []
    assert not any("datasets" in str(v) for v in services["app"].get("volumes") or [])


def test_verify_security_config_rejects_a_writable_web_mount_or_a_stray_mount(
    verify: Any, compose: dict[str, Any]
) -> None:
    check = verify.check_compose
    assert check(compose) == []
    bad = copy.deepcopy(compose)
    bad["services"]["app"]["volumes"] = ["review_packages:/review"]
    assert any("read-only" in p for p in check(bad))
    stray = copy.deepcopy(compose)
    volumes = stray["services"]["grafana"].get("volumes", [])
    stray["services"]["grafana"]["volumes"] = [*volumes, "review_packages:/x"]
    assert any("must not mount review_packages" in p for p in check(stray))
