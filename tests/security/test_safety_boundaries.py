"""Phase 8 boundaries: no real order path, no credentials, no live, private reads only through the
adapter, the web tier reaches only the database-only control service. Static + behavioural."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from app import constants
from app.domain.enums import AuditEventType
from app.exchange import factory, sandbox
from app.exchange.coinbase_private import NullSigner
from app.monitoring.collectors import _CLASS_OF
from app.safety import live_gate
from app.safety.types import BLOCK_REASONS, VENUES
from app.web import bot_views
from tests.security.test_pair_no_exchange_access import APP, imports_of

EXCHANGE = APP / "exchange"
SAFETY = APP / "safety"
TEMPLATES = APP / "web" / "templates"
WEB_TIER = ("api", "web", "auth", "monitoring")
NETWORK = (
    "socket",
    "requests",
    "urllib.request",
    "urllib3",
    "http.client",
    "aiohttp",
    "websockets",
    "ftplib",
    "smtplib",
    "subprocess",
    "asyncio.subprocess",
)
CRYPTO = ("jwt", "jose", "cryptography", "base64", "nacl", "Crypto", "ecdsa", "rsa")
UNSAFE = ("yaml", "pickle", "marshal", "shelve", "dill")


def sources(*dirs: Path) -> dict[Path, str]:
    return {p: p.read_text() for d in dirs for p in sorted(d.glob("*.py"))}


def code_words(source: str) -> set[str]:
    tree = ast.parse(source)
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                doc_ids.add(id(first.value))
    words: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            words.add(n.id)
        elif isinstance(n, ast.Attribute):
            words.add(n.attr)
        elif isinstance(n, ast.FunctionDef | ast.ClassDef):
            words.add(n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            words.add(n.value)
    return words


# ------------------------------------------------------------------ the web tier
def test_the_web_tier_never_imports_the_exchange_boundary_or_host_only_safety_code() -> None:
    allowed = {"app.safety.control", "app.safety.live_gate", "app.safety.types"}
    for folder in WEB_TIER:
        for path, source in sources(APP / folder).items():
            mods = imports_of(source)
            assert not [m for m in mods if m.startswith("app.exchange")], path
            bad = [m for m in mods if m.startswith("app.safety") and m not in allowed]
            assert not bad, (path, bad)


def test_the_control_service_is_database_only_code() -> None:
    for name in ("control.py", "live_gate.py", "types.py"):
        mods = imports_of((SAFETY / name).read_text())
        assert not [
            m
            for m in mods
            if m.startswith(("app.exchange", "app.market", "app.paper", "app.adapters", "httpx"))
        ], name
        assert not [m for m in mods if m.startswith(NETWORK)], name


def test_only_the_host_modules_reference_submit_or_cancel_calls() -> None:
    users = {
        p.name
        for p, s in sources(SAFETY, EXCHANGE, APP / "api", APP / "web").items()
        if "submit" in code_words(s) or "cancel" in code_words(s)
    }
    assert users <= {"pipeline.py", "gateway.py", "fake.py", "commands.py"}, users


def test_bot_routes_and_views_carry_no_order_creation() -> None:
    text = (
        (APP / "api" / "bot.py").read_text()
        + (APP / "web" / "bot_views.py").read_text()
        + (SAFETY / "control.py").read_text()
    )
    for word in (
        "create_order",
        "place_order",
        "submit(",
        "OrderRequest",
        "gateway",
        "gateways",
        "market_market",
        "OrderPipeline",
        "Reconciler",
    ):
        assert word not in text, word


# ------------------------------------------------------------------ no network or credentials outside the reader
def test_safety_and_exchange_code_imports_no_process_or_unsafe_deserialisation_code() -> None:
    for path, source in sources(SAFETY, EXCHANGE).items():
        mods = imports_of(source)
        assert not [m for m in mods if m.startswith(NETWORK)], path
        assert not [m for m in mods if m.split(".")[0] in UNSAFE], path


# The ONLY modules that may sign a request or read a key file (Phase 10, DEC-026). Anything else in
# the safety, exchange, API, auth or web code that imports crypto or reads files/env fails below.
SIGNING_MODULES = {"cdp_signer.py", "credentials.py"}
HTTP_MODULES = {"coinbase_private.py", "coinbase_live.py"}  # the read adapter and the live gateway


def test_only_the_exchange_adapters_use_an_http_client_and_only_two_modules_sign() -> None:
    http_users = {p.name for p, s in sources(SAFETY, EXCHANGE).items() if "httpx" in imports_of(s)}
    assert "coinbase_private.py" in http_users and http_users <= HTTP_MODULES
    crypto_users = set()
    for path, source in sources(SAFETY, EXCHANGE).items():
        if [m for m in imports_of(source) if m.split(".")[0] in CRYPTO]:
            crypto_users.add(path.name)
        if path.name != "credentials.py":  # the one module that reads the key file
            assert not re.search(r"os\.environ|getenv|read_text\(|open\(", source), path
    assert crypto_users <= SIGNING_MODULES  # a stray signer elsewhere would fail here


def test_the_web_tier_never_imports_the_signing_or_credential_modules() -> None:
    web = (APP / "web", APP / "api", APP / "auth", SAFETY)
    for path, source in sources(*web).items():
        if path.name in ("factory.py",):
            continue
        mods = imports_of(source)
        assert not [
            m for m in mods if m.startswith(("app.exchange.credentials", "app.exchange.cdp_signer"))
        ], path


def test_only_the_null_signer_and_the_cdp_signer_exist_and_the_null_one_refuses() -> None:
    signers = sorted(
        p.name
        for p, s in sources(
            APP / "adapters", APP / "exchange", APP / "safety", APP / "api", APP / "auth"
        ).items()
        if re.search(r"class \w*Signer", s)
    )
    assert signers == ["cdp_signer.py", "coinbase_private.py"]
    source = (EXCHANGE / "coinbase_private.py").read_text()
    assert len(re.findall(r"class \w*Signer", source)) == 2  # the protocol and the null signer
    assert len(re.findall(r"class \w*Signer", (EXCHANGE / "cdp_signer.py").read_text())) == 1
    from app.exchange.errors import NoCredentials

    try:
        NullSigner().bearer("GET", "api.coinbase.com", "/x")
    except NoCredentials:
        pass
    else:  # pragma: no cover
        raise AssertionError("the null signer signed something")


def test_no_reader_and_no_gateway_exist_in_any_deployment_of_this_build(
    settings: Any = None,
) -> None:
    from app.config import load_settings

    cfg = load_settings({"TD_ENVIRONMENT": "development", "TD_SECRET_KEY": "x" * 48})
    assert factory.build_reader(cfg) is None and factory.build_gateway(cfg) is None
    text = (EXCHANGE / "factory.py").read_text()
    assert not re.search(r"os\.environ|getenv|open\(|read_text", text)


def test_the_public_and_private_modules_keep_their_own_paths() -> None:
    private = (EXCHANGE / "coinbase_private.py").read_text()
    public = (APP / "adapters" / "coinbase_public.py").read_text()
    assert "/orders/historical" in private and "/accounts" in private
    for word in ("/orders", "/accounts", "key_permissions", "Authorization", "Bearer"):
        assert word not in public


def test_the_egress_proxy_allowlist_is_still_one_host() -> None:
    from app.adapters.egress_proxy import ALLOWED

    assert ALLOWED == frozenset({("api.coinbase.com", 443)})


# ------------------------------------------------------------------ one order shape
def test_the_only_order_that_can_be_expressed_is_a_post_only_limit_gtc_order() -> None:
    from app.exchange.gateway import OrderRequest

    assert set(OrderRequest.__dataclass_fields__) == {
        "client_order_id",
        "product_id",
        "side",
        "price",
        "base_qty",
    }
    words = code_words((EXCHANGE / "gateway.py").read_text())
    for word in (
        "market_market_ioc",
        "stop_limit",
        "leverage",
        "margin_type",
        "attached_order",
        "trigger",
    ):
        assert word not in words


def test_the_sandbox_module_can_only_check_shapes() -> None:
    public = {
        n
        for n in dir(sandbox)
        if not n.startswith("_")
        and callable(getattr(sandbox, n))
        and getattr(getattr(sandbox, n), "__module__", "") == sandbox.__name__
    }
    assert public == {"check_request_shape", "check_recorded_response"}


def test_the_fake_exchange_has_no_matching_engine() -> None:
    from app.exchange.fake import FakeExchange

    methods = {n for n in dir(FakeExchange) if not n.startswith("_")}
    assert not {n for n in methods if re.search(r"match|simulate|tick|advance|fill_all|auto", n)}
    source = (EXCHANGE / "fake.py").read_text()
    assert "TEST-DOUBLE" in source and "never matches" in source


# ------------------------------------------------------------------ money and integrity
def test_no_float_arithmetic_in_money_code() -> None:
    for name in (
        "risk_engine.py",
        "ledger.py",
        "context.py",
        "pipeline.py",
        "reconciler.py",
        "anomaly.py",
        "control.py",
        "monitor.py",
        "commands.py",
        "types.py",
    ):
        tree = ast.parse((SAFETY / name).read_text())
        assert not [
            n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)
        ], name
        assert not [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"
        ], name
    tree = ast.parse((EXCHANGE / "models.py").read_text())
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"
    ]


def test_all_safety_sql_writes_target_only_safety_tables() -> None:
    source = (APP / "storage" / "safety_repositories.py").read_text()
    writes = set(re.findall(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([a-z_]+)", source))
    assert writes <= {
        "bot_control",
        "reconciliation_runs",
        "reconciliation_findings",
        "venue_baselines",
        "order_intents",
        "risk_decisions",
        "order_attempts",
        "attempt_fills",
        "order_hints",
        "api_events",
        "control_commands",
        "live_attestations",  # Phase 10: host-written, append-only
        "live_arming",  # Phase 10: host-written, revoke-only, at most 24 hours
    }, writes
    assert "TRUNCATE" not in source and "DROP" not in source


def test_pipeline_and_recovery_never_touch_pairs_paper_or_config() -> None:
    text = "".join(
        (SAFETY / n).read_text()
        for n in (
            "pipeline.py",
            "reconciler.py",
            "recovery.py",
            "commands.py",
            "monitor.py",
            "host_control.py",
        )
    )
    for word in (
        "simple_action",
        "PairService",
        "apply_transition",
        "paper_orders",
        "runtime_configs",
        "write_env",
        "os.system",
    ):
        assert word not in text, word


def test_every_block_reason_has_view_text_and_a_metric_class() -> None:
    for code in BLOCK_REASONS:
        assert code in bot_views.REASON_TEXT, code
        assert _CLASS_OF.get(code, "other") != "other" or code == "INPUTS_UNAVAILABLE", code


def test_the_phase8_audit_events_are_in_the_catalogue() -> None:
    have = {e.value for e in AuditEventType}
    assert {
        "bot.control_requested", "bot.control_denied", "bot.paused", "bot.resumed", "bot.kill_activated",
        "bot.kill_released", "bot.breaker_opened", "bot.recovery_started", "bot.recovery_completed",
        "bot.cancel_requested", "bot.cancel_completed", "bot.cancel_failed", "reconciliation.ok",
        "reconciliation.mismatch", "reconciliation.failed", "order.intent_created", "order.risk_allowed",
        "order.risk_blocked", "order.attempt_authorized", "order.submitting", "order.submitted",
        "order.rejected", "order.unknown", "order.resolved", "order.absent",
    } <= have  # fmt: skip


# ------------------------------------------------------------------ live stays blocked
def test_live_is_unrepresentable_in_modes_venues_and_the_gate() -> None:
    assert "LIVE" not in constants.ALLOWED_MODES and constants.LIVE_TRADING_STATUS == "BLOCKED"
    # Phase 10 (DEC-026): COINBASE is a representable venue, but live is never unconditional: no LIVE
    # mode or pair state exists, the default gate is BLOCKED, and orders need a host arming.
    assert VENUES == ("PAPER", "FAKE", "COINBASE") and live_gate.LIVE_UNBLOCK_PRESENT is False
    assert live_gate.panel().status == "BLOCKED"
    assert live_gate.order_gate("COINBASE", object(), live_armed=False)[0] is False  # type: ignore[arg-type]
    for name in ("0006_safety.sql", "0009_live_venue.sql"):
        sql = (APP / "storage" / "migrations" / name).read_text()
        assert not re.search(r"IN \([^)]*'LIVE'[^)]*\)", sql.replace("'LIVE_READ'", ""))


def test_the_gate_panel_is_static_html_with_no_control() -> None:
    for name in ("bot.html", "bot_confirm.html"):
        text = (TEMPLATES / name).read_text()
        assert "|safe" not in text and "| safe" not in text and "autoescape false" not in text
        assert not re.search(r"<script(?![^>]*\bsrc=)", text) and not re.search(
            r"\son[a-z]+\s*=", text
        )
    assert "unlock" not in (TEMPLATES / "bot.html").read_text().lower()


def test_deployment_files_carry_no_exchange_credentials() -> None:
    root = APP.parent
    for name in ("docker-compose.yml", ".env.example", "Dockerfile"):
        text = (root / name).read_text().upper()
        for word in ("COINBASE_API", "API_KEY", "JWT", "PRIVATE_KEY", "CDP_", "ORGANIZATIONS/"):
            assert word not in text, (name, word)
