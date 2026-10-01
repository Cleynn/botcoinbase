"""`live` host CLI: status, check, baseline, arm, disarm, config and a bounded run. The exchange is
the scripted FAKE double (its venue is FAKE, so arming is exercised through the same commands)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.batch_cli import main
from app.exchange.models import KeyPermissions
from app.live.cli import ARM_PHRASE, BASELINE_PHRASE
from tests.conftest import TestDb
from tests.integration.safety_env import SafetyEnv


def run(safe: SafetyEnv, db: TestDb, args: list[str], out: list[str], **kw: Any) -> int:
    settings = safe.settings.model_copy(update={"database": db.settings_for("td_ctl")})
    return main(
        ["live", *args],
        settings=settings,
        storage=safe.ctl,
        clock=safe.clock,
        out=out.append,
        sleep=lambda _s: None,
        **kw,
    )


def test_status_and_config_show(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert run(safe, db, ["status"], out) == 0
    text = "\n".join(out)
    assert "mode=PAPER live_armed=False" in text and "LIVE configuration: pairs=1" in text
    out.clear()
    assert run(safe, db, ["config"], out) == 0 and out[0].startswith("pairs=1 levels=3")


def test_config_edit_validates_and_needs_the_bot_paused(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    args = ["config", "--pairs", "3", "--levels", "6", "--per-grid", "20"]
    args += ["--invested", "60", "--reserve", "20", "--per-order", "10"]
    assert run(safe, db, args, out) == 0
    assert out[0].startswith("updated: pairs=3 levels=6")
    out.clear()
    assert run(safe, db, ["config", "--pairs", "11"], out) == 1 and "MAX_PAIRS" in out[0]
    out.clear()
    assert run(safe, db, ["config", "--reserve", "1"], out) == 1 and "RESERVE_BELOW_FLOOR" in out[0]
    out.clear()
    assert run(safe, db, ["config", "--per-grid", "abc"], out) == 1 and "AMOUNTS" in out[0]


def test_without_credentials_the_exchange_commands_say_so(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert run(safe, db, ["check"], out) == 1 and "NO_CREDENTIALS" in out[0]


def test_check_reports_permissions_and_balance_without_secrets(safe: SafetyEnv, db: TestDb) -> None:
    safe.fake.set_balance("USDC", "50")
    out: list[str] = []
    assert run(safe, db, ["check"], out, reader=safe.fake) == 0
    text = "\n".join(out)
    assert "USDC available=50" in text and "transfer=False" in text


def test_a_key_that_can_transfer_is_refused_for_arming(safe: SafetyEnv, db: TestDb) -> None:
    class Risky:
        venue = "FAKE"

        def key_permissions(self) -> KeyPermissions:
            return KeyPermissions(True, True, True, "SPOT")

        def list_accounts(self) -> tuple[Any, ...]:
            return ()

    out: list[str] = []
    assert run(safe, db, ["arm", "--confirm", ARM_PHRASE], out, reader=Risky()) == 1
    assert "KEY_PERMISSIONS" in out[0]


def test_baseline_needs_the_phrase_and_is_recorded_once(safe: SafetyEnv, db: TestDb) -> None:
    safe.fake.set_balance("USDC", "50")
    out: list[str] = []
    assert run(safe, db, ["baseline", "--confirm", "nope"], out, reader=safe.fake) == 1
    out.clear()
    assert run(safe, db, ["baseline", "--confirm", BASELINE_PHRASE], out, reader=safe.fake) == 0
    with safe.ctl.tx() as repos:
        assert repos.safety.baselines("COINBASE")["USDC"] == Decimal(50)
    out.clear()
    assert run(safe, db, ["baseline", "--confirm", BASELINE_PHRASE], out, reader=safe.fake) == 1
    assert "BASELINE_EXISTS" in out[0]


def test_arm_without_the_phrase_or_with_bad_hours_is_refused(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert run(safe, db, ["arm", "--confirm", "x"], out, reader=safe.fake) == 1
    out.clear()
    assert (
        run(safe, db, ["arm", "--hours", "25", "--confirm", ARM_PHRASE], out, reader=safe.fake) == 1
    )
    assert "HOURS_OUT_OF_RANGE" in out[0]


def test_disarm_with_nothing_armed_is_harmless(safe: SafetyEnv, db: TestDb) -> None:
    out: list[str] = []
    assert run(safe, db, ["disarm"], out) == 0 and "revoked 0" in out[0]


def test_run_recovers_pauses_and_idles_until_the_bot_is_resumed(
    safe: SafetyEnv, db: TestDb
) -> None:
    safe.baseline("50")
    out: list[str] = []
    code = run(safe, db, ["run", "--max-ticks", "2", "--interval", "1"], out,
               reader=safe.fake, gateway=safe.fake)  # fmt: skip
    assert code == 0
    text = "\n".join(out)
    assert "recovery complete=True" in text and "the bot is PAUSED" in text
    assert "tick 1: idle (MODE_NOT_LIVE)" in text and "tick 2: idle" in text
    assert not safe.fake.orders  # nothing is placed while the mode is not LIVE
