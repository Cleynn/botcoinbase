"""Phase 5 boundaries: no network in the simulation, no private API anywhere, no floats."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.security.test_pair_no_exchange_access import APP, imports_of, sources

# database-only or pure (no I/O) modules that the web tier may use
PURE = {"app.paper.gate", "app.market.candles"}
SIMULATION = ("backtest", "strategy", "paper", "reports", "domain")
NETWORK = ("httpx", "requests", "urllib", "socket", "http.client", "aiohttp", "app.adapters")
PRIVATE = (
    "/orders",
    "/accounts",
    "/portfolios",
    "/key_permissions",
    "create_order",
    "cancel_order",
    "cb-access",
    "passphrase",
    "private_key",
    "api_key",
    "jwt",
    "secret_key",
    "websocket",
)


def test_simulation_packages_import_no_network_code() -> None:
    for path, source in sources(*SIMULATION).items():
        bad = [m for m in imports_of(source) if m.startswith(NETWORK)]
        assert not bad, (path, bad)


def test_only_the_importer_and_the_runners_use_the_public_client() -> None:
    users = {
        str(p.relative_to(APP))
        for p, s in sources(
            *(d.name for d in APP.iterdir() if d.is_dir() and d.name != "__pycache__")
        ).items()
        if any(m.startswith("app.adapters.coinbase_public") for m in imports_of(s))
    }
    assert users == {"market/ingest.py", "pairs/runner.py", "pairs/cli.py"}


def test_the_web_tier_never_imports_market_paper_backtest_or_strategy_code() -> None:
    banned = ("app.market", "app.paper", "app.backtest", "app.strategy", "app.adapters")
    for path, source in sources("api", "web", "auth", "monitoring").items():
        bad = [m for m in imports_of(source) if m.startswith(banned) and m not in PURE]
        assert not bad, (path, bad)  # the paper gate is database-only code


def test_no_private_api_order_or_credential_word_in_phase5_code() -> None:
    for path, source in sources("market", "backtest", "strategy", "paper", "reports").items():
        tree = ast.parse(source)
        strings = [
            n.value.lower()
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 200
        ]
        for word in PRIVATE:
            assert not [s for s in strings if word in s], (path, word)
        names = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"api_key", "private_key", "passphrase"}, path


def _sleep_arguments(tree: ast.AST) -> set[int]:
    """`time.sleep` needs a float; that one conversion (of a delay, not money) is allowed."""
    return {
        id(arg)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_sleep"
        for arg in n.args
    }


def test_financial_modules_use_no_float_arithmetic() -> None:
    for path, source in sources("backtest", "strategy", "paper", "market", "domain").items():
        tree = ast.parse(source)
        allowed = _sleep_arguments(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                raise AssertionError((path, node.lineno, "float literal"))
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "float"
                and id(node) not in allowed
            ):
                raise AssertionError((path, node.lineno, "float() call"))
            if isinstance(node, ast.Name) and node.id == "math":
                raise AssertionError((path, node.lineno, "math module"))


def test_paper_order_code_never_names_a_real_venue() -> None:
    text = (APP / "paper" / "exchange.py").read_text().lower()
    assert "coinbase" not in text.replace("exchange", "")  # docstrings say "any exchange" only


def test_growth_and_regridding_are_disabled_in_the_shipped_policy() -> None:
    import yaml

    from tests.conftest import ROOT

    policy = yaml.safe_load(Path(ROOT / "config" / "pair-policy.yaml").read_text())
    assert policy["capital_growth_enabled"] is False
    assert policy.get("regridding_enabled", False) is False
