"""Pair operations never reach a private Coinbase API, an order, a credential or the network from
the web tier. Source-level and behavioural guarantees."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from tests.conftest import ROOT, walk_routes

APP = ROOT / "app"
FORBIDDEN_WORDS = (
    "/orders",
    "/accounts",
    "/portfolios",
    "/key_permissions",
    "create_order",
    "cancel_order",
    "cancel_orders",
    "client_order_id",
    "order_intent",
    "post_only=true",
    "cb-access",
    "passphrase",
    "private_key",
    "api_key",
    "jwt",
)


def sources(*folders: str) -> dict[Path, str]:
    return {
        p: p.read_text()
        for folder in folders
        for p in (APP / folder).rglob("*.py")
        if "__pycache__" not in p.parts
    }


def imports_of(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_web_tier_never_imports_the_exchange_client_or_http_libraries() -> None:
    for path, source in sources("api", "web", "auth", "monitoring").items():
        mods = imports_of(source)
        assert not [
            m for m in mods if m.startswith(("app.adapters", "httpx", "requests", "urllib3"))
        ], path
    # the pair service and its storage are pure database code
    for name in ("service.py", "transitions.py", "policy.py", "validation.py"):
        mods = imports_of((APP / "pairs" / name).read_text())
        assert not [m for m in mods if m.startswith(("httpx", "requests", "socket"))], name
    assert "app.adapters" not in imports_of((APP / "pairs" / "service.py").read_text())


def test_only_the_runner_and_its_cli_touch_the_exchange_adapter() -> None:
    users = {
        str(p.relative_to(APP))
        for p, s in sources(
            "api", "auth", "domain", "monitoring", "pairs", "storage", "web"
        ).items()
        if any(m.startswith("app.adapters.coinbase_public") for m in imports_of(s))
    }
    assert users == {"pairs/runner.py", "pairs/cli.py"}


def test_no_module_contains_private_api_order_or_credential_code() -> None:
    for path, source in sources("adapters", "pairs", "api", "web").items():
        tree = ast.parse(source)
        strings = [
            n.value.lower()
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        names = [n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)]
        names += [n.attr.lower() for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
        blob = "\n".join(strings + names)
        for word in FORBIDDEN_WORDS:
            assert word not in blob, f"{word!r} in {path}"


def test_the_adapter_package_has_no_credential_loading_or_signing() -> None:
    for path, source in sources("adapters").items():
        assert not re.search(
            r"os\.environ|getenv|open\(|read_text|hmac|hashlib\.sha256\(.*key", source
        ), path
        assert not [
            m
            for m in imports_of(source)
            if m.split(".")[0] in {"jwt", "cryptography", "hmac", "base64"}
        ]


def test_no_pair_route_can_create_or_cancel_an_order(app: Any) -> None:
    paths = [r.path for r in walk_routes(app)]
    assert not [
        p for p in paths if re.search(r"order|fill|account|balance|exchange|coinbase|kill|bot", p)
    ]
    for route in walk_routes(app):
        if route.path.startswith("/pairs"):
            assert route.methods <= {"GET", "POST", "HEAD"}


def test_pair_operations_make_no_network_calls(
    env: Any, admin_client: Any, monkeypatch: Any
) -> None:
    """Every pair action available on the web tier runs with sockets forbidden."""
    import socket

    from app.domain.pairs import PairAction, PairState

    pair_id = env.eligible("BTC-USDC")
    real = socket.socket.connect

    def guard(self: Any, address: Any) -> Any:
        if (
            isinstance(address, tuple)
            and address[0] not in ("127.0.0.1", "::1", "localhost")
            and not str(address[0]).startswith("/")
        ):
            raise AssertionError(f"network access attempted: {address!r}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guard)
    env.coinbase.requests.clear()
    from tests.integration.test_pair_web import confirm, reauth

    for path in (
        "/pairs",
        "/pairs/products",
        f"/pairs/{pair_id}",
        f"/pairs/{pair_id}/archive/request",
    ):
        assert admin_client.get(path).status_code == 200
    version = env.get(pair_id).version
    reauth(admin_client, pair_id, "archive", version)
    assert (
        confirm(admin_client, pair_id, "archive", version, "ARCHIVE PAIR BTC-USDC").status_code
        == 303
    )
    assert env.state(pair_id) is PairState.ARCHIVED
    assert env.coinbase.requests == []  # the web tier never asked Coinbase anything
    assert PairAction.ARCHIVE


def test_the_exchange_module_only_names_the_fixed_public_host() -> None:
    hosts = set(
        re.findall(
            r"https?://[A-Za-z0-9.-]+", "\n".join(sources("adapters", "pairs", "api").values())
        )
    )
    assert hosts <= {"https://api.coinbase.com", "http://"} or all(
        "api.coinbase.com" in h for h in hosts
    )
