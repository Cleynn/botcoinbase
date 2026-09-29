"""`safety` host CLI (td_ctl only). It offers no way to resume, submit or sell anything."""

from __future__ import annotations

from typing import Any

import pytest

from app.batch_cli import _parser, main
from tests.conftest import TestDb
from tests.integration.safety_env import SafetyEnv


def run(
    safe: SafetyEnv,
    db: TestDb,
    args: list[str],
    out: list[str],
    *,
    role: str = "td_ctl",
    reader: Any = None,
    gateway: Any = None,
) -> int:
    settings = safe.settings.model_copy(update={"database": db.settings_for(role)})
    return main(
        ["safety", *args],
        settings=settings,
        storage=safe.ctl,
        clock=safe.clock,
        out=out.append,
        reader=reader,
        gateway=gateway,
    )


def test_status_reports_the_blocked_gate_and_the_control_state(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert run(safe, db, ["status"], out) == 0
    text = "\n".join(out)
    assert (
        "LIVE TRADING: BLOCKED" in text
        and "bot=PAUSED kill_switch=INACTIVE breaker=CLOSED recovery=INCOMPLETE" in text
    )
    assert (
        "reconciliation: none" in text
        and "resume blockers: " in text
        and "RECOVERY_INCOMPLETE" in text
    )


def test_without_an_exchange_reader_reconcile_and_recover_say_so_and_do_nothing(
    safe: SafetyEnv, db: TestDb
) -> None:
    out: list[str] = []
    assert run(safe, db, ["reconcile"], out) == 1 and "NO_EXCHANGE_READER" in out[0]
    out.clear()
    assert (
        run(safe, db, ["recover"], out) == 1
        and "NO_EXCHANGE_READER" in out[0]
        and "stays PAUSED" in out[0]
    )
    row = safe.control_row()
    assert (row.bot_state, row.recovery_state) == ("PAUSED", "INCOMPLETE")


def test_with_a_reader_recovery_completes_and_still_leaves_the_bot_paused(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.baseline()
    out: list[str] = []
    assert run(safe, db, ["recover"], out, reader=safe.fake) == 0
    assert "complete=True" in out[0] and "stays PAUSED" in out[0]
    assert safe.control_row().bot_state == "PAUSED"
    out.clear()
    assert (
        run(safe, db, ["reconcile"], out, reader=safe.fake) == 0 and "reconciliation OK" in out[0]
    )


def test_a_mismatch_exits_nonzero_and_names_the_findings(safe: SafetyEnv, db: TestDb) -> None:
    safe.baseline()
    safe.fake.add_foreign_order()
    out: list[str] = []
    assert run(safe, db, ["reconcile"], out, reader=safe.fake) == 1 and "UNKNOWN_ORDER" in out[0]


def test_monitor_reports_and_opens_the_breaker(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert run(safe, db, ["monitor"], out) == 0 and out == ["breaker unchanged"]
    with safe.ctl.tx() as repos:
        for _ in range(5):
            repos.safety.add_api_event("FAKE", "list_orders", False, "TIMEOUT", safe.clock.now())
    out.clear()
    assert run(safe, db, ["monitor"], out) == 0 and out == ["breaker OPENED: API_FAILURES"]


def test_commands_run_the_queued_cancel_through_the_gateway(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert run(safe, db, ["commands"], out) == 0 and out == ["no pending command"]
    safe.act("kill")
    out.clear()
    assert run(safe, db, ["commands"], out, gateway=safe.fake) == 0 and "DONE" in out[0]


def test_pause_kill_and_release_need_their_phrases(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert (
        run(safe, db, ["kill", "--confirm", "activate kill switch"], out) == 1
        and safe.control_row().kill_switch == "INACTIVE"
    )
    assert (
        run(safe, db, ["kill", "--confirm", "ACTIVATE KILL SWITCH"], out) == 0
        and safe.control_row().kill_switch == "ACTIVE"
    )
    assert run(safe, db, ["kill-release", "--confirm", "wrong"], out) == 1
    assert run(safe, db, ["kill-release", "--confirm", "RELEASE KILL SWITCH"], out) == 0
    row = safe.control_row()
    assert (row.kill_switch, row.bot_state, row.recovery_state) == (
        "INACTIVE",
        "PAUSED",
        "INCOMPLETE",
    )
    assert any("the bot stays PAUSED" in line for line in out)


def test_the_web_role_cannot_run_the_safety_cli(
    safe: SafetyEnv, db: TestDb, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(safe, db, ["status"], [], role="td_app") == 2
    assert "host CLI role" in capsys.readouterr().err


def test_the_cli_has_exactly_the_reviewed_commands_and_no_way_to_resume_submit_or_sell(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["safety", "--help"])
    listed = capsys.readouterr().out.split("{")[1].split("}")[0].split(",")
    assert set(listed) == {
        "status",
        "recover",
        "reconcile",
        "monitor",
        "commands",
        "pause",
        "kill",
        "kill-release",
        "prune",
    }
    for forbidden in (
        "resume",
        "submit",
        "order",
        "sell",
        "buy",
        "place",
        "cancel-all",
        "liquidate",
        "live",
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["safety", forbidden])
        capsys.readouterr()
