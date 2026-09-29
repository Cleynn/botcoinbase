"""The host CLI: `python -m app.cli pairs ...` (public data only, td_ctl only)."""

from __future__ import annotations

from typing import Any

import pytest

from app.cli import main as cli_main
from app.domain.pairs import PairState
from app.pairs.cli import main as pairs_main
from app.storage.database import Storage


def run(args: list[str], env: Any, settings: Any, ctl_storage: Storage, clock: Any) -> int:
    ctl_settings = settings.model_copy(
        update={"database": settings.database.model_copy(update={"user": "td_ctl"})}
    )
    return pairs_main(
        args,
        settings=ctl_settings,
        storage=ctl_storage,
        client=env.coinbase.client(),
        clock=clock,
    )


def test_discover_seed_validate_and_list(
    env: Any, settings: Any, ctl_storage: Storage, clock: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["discover"], env, settings, ctl_storage, clock) == 0
    assert "discovery: seen=3 usdc_spot=3 stored=3 new=3" in capsys.readouterr().out
    assert run(["seed", "--queue-validation"], env, settings, ctl_storage, clock) == 0
    out = capsys.readouterr().out
    assert out.count("PROPOSED") == 3
    assert run(["validate"], env, settings, ctl_storage, clock) == 0
    out = capsys.readouterr().out
    assert out.count("PAPER_ELIGIBLE (PASS)") == 3 and "expired eligibility: 0" in out
    assert run(["list"], env, settings, ctl_storage, clock) == 0
    assert capsys.readouterr().out.count("PAPER_ELIGIBLE v3") == 3
    with env.storage.tx() as repos:
        assert not [p for p in repos.pairs.list_all() if p.state is PairState.PAPER_ACTIVE]


def test_validate_with_nothing_queued_says_so(
    env: Any, settings: Any, ctl_storage: Storage, clock: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["validate"], env, settings, ctl_storage, clock) == 0
    assert "nothing queued" in capsys.readouterr().out


def test_the_web_role_is_refused_before_anything_happens(
    env: Any, settings: Any, storage: Storage, capsys: pytest.CaptureFixture[str]
) -> None:
    code = pairs_main(
        ["discover"], settings=settings, storage=storage, client=env.coinbase.client()
    )
    assert code == 2 and "host CLI role" in capsys.readouterr().err
    assert env.coinbase.requests == []


def test_a_failed_run_reports_a_fixed_code_and_exits_nonzero(
    env: Any, settings: Any, ctl_storage: Storage, clock: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    env.coinbase.errors["/market/products"] = 500
    assert run(["discover"], env, settings, ctl_storage, clock) == 1
    assert "PRODUCTS_HTTP_ERROR" in capsys.readouterr().err


def test_the_top_level_cli_routes_pairs_and_has_no_trading_commands() -> None:
    with pytest.raises(SystemExit):
        cli_main(["order"])
    with pytest.raises(SystemExit):
        pairs_main(["activate"])
    with pytest.raises(SystemExit):
        pairs_main(["archive"])
