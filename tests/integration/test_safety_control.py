"""The ADMIN control workflow (pause, resume, cancel known orders, kill switch), its audit trail and
its refusals. The service is database-only code: it cannot reach an exchange or create an order."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.exchange.fake import Fault
from app.safety.control import ACTIONS, PHRASES
from tests.integration.safety_env import BOT_TABLES, SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]


def fingerprint(sql: Sql) -> dict[str, Any]:
    return {
        t: sql(
            f"SELECT count(*) AS n, md5(string_agg(x::text, ',' ORDER BY x::text)) AS h FROM {t} x"
        )[0]  # noqa: S608
        for t in BOT_TABLES
    }


def events(sql: Sql, prefix: str = "bot.") -> list[str]:
    return [
        r["event_code"]
        for r in sql(
            "SELECT event_code FROM audit_events WHERE event_code LIKE %s ORDER BY seq",
            (prefix + "%",),
        )
    ]


def test_the_four_phrases_are_exactly_the_specified_ones() -> None:
    assert PHRASES == {
        "pause": "PAUSE BOT",
        "resume": "RESUME BOT AFTER RECONCILIATION",
        "cancel_known": "CANCEL KNOWN BOT ORDERS",
        "kill": "ACTIVATE KILL SWITCH",
    }
    assert set(ACTIONS) == set(PHRASES) and len(ACTIONS) == 4


# ------------------------------------------------------------------ the chain
@pytest.mark.parametrize("action", ACTIONS)
def test_a_wrong_phrase_is_refused_without_spending_the_reauth(
    safe: SafetyEnv, sql: Sql, action: str
) -> None:
    safe.reauth.available = True
    out = safe.control.execute(safe.ctx, safe.actor, action, PHRASES[action].lower())
    assert out.kind == "phrase_mismatch" and safe.reauth.consumed == 0 and safe.reauth.available
    assert events(sql) == ["bot.control_denied"]
    row = sql(
        "SELECT result, reason_code FROM audit_events WHERE event_code = 'bot.control_denied'"
    )[0]
    assert (row["result"], row["reason_code"]) == ("DENIED", "PHRASE_MISMATCH")


@pytest.mark.parametrize("action", ACTIONS)
def test_without_a_fresh_reauth_nothing_happens(safe: SafetyEnv, sql: Sql, action: str) -> None:
    before = sql("SELECT version FROM bot_control")[0]["version"]
    out = safe.act(action, fresh=False)
    assert out.kind == "reauth_required"
    assert sql("SELECT version FROM bot_control")[0]["version"] == before
    assert events(sql) == ["bot.control_denied"]


def test_the_reauth_is_single_use(safe: SafetyEnv) -> None:
    safe.running()
    assert safe.act("pause").kind == "ok"
    again = safe.control.execute(safe.ctx, safe.actor, "pause", PHRASES["pause"])
    assert again.kind == "reauth_required"


def test_an_unknown_action_is_invalid(safe: SafetyEnv) -> None:
    assert (
        safe.control.execute(safe.ctx, safe.actor, "sell_everything", "SELL EVERYTHING").kind
        == "invalid"
    )


def test_a_phrase_must_match_byte_for_byte(safe: SafetyEnv) -> None:
    for typed in (
        "PAUSE BOT ",
        " PAUSE BOT",
        "Pause Bot",
        "PAUSE  BOT",
        "PAUSE BOT\n",
        "ＰＡＵＳＥ ＢＯＴ",
        "PAUSE BOT​",
        "",
    ):
        safe.reauth.available = True
        assert (
            safe.control.execute(safe.ctx, safe.actor, "pause", typed).kind == "phrase_mismatch"
        ), repr(typed)
    assert safe.reauth.consumed == 0


def test_an_accepted_request_is_audited_before_its_outcome(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    sql("SELECT 1")
    before = len(events(sql))
    assert safe.act("pause").kind == "ok"
    assert events(sql)[before:] == ["bot.control_requested", "bot.paused"]
    requested = sql(
        "SELECT actor_role, target_type, detail FROM audit_events WHERE event_code = 'bot.control_requested'"
    )[-1]
    assert requested["actor_role"] == "ADMIN" and requested["target_type"] == "bot"


def test_audit_details_never_carry_credentials_or_phrases(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    safe.act("kill")
    blob = " ".join(str(r) for r in sql("SELECT * FROM audit_events WHERE event_code LIKE 'bot.%'"))
    assert "password" not in blob.lower() and "ACTIVATE KILL SWITCH" not in blob


# ------------------------------------------------------------------ pause
def test_pause_stops_a_running_bot(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    assert safe.act("pause").kind == "ok"
    assert safe.control_row().bot_state == "PAUSED"
    out = safe.act("pause")
    assert out.kind == "not_allowed" and out.reasons == ("ALREADY_PAUSED",)
    assert events(sql)[-2:] == ["bot.control_requested", "bot.control_denied"]


# ------------------------------------------------------------------ kill switch
def test_the_kill_switch_pauses_blocks_and_queues_a_cancel_but_never_sells(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    before = fingerprint(sql)
    assert safe.act("kill").kind == "ok"
    row = safe.control_row()
    assert (row.bot_state, row.kill_switch, row.kill_reason) == (
        "PAUSED",
        "ACTIVE",
        "OPERATOR_KILL",
    )
    (cmd,) = sql("SELECT kind, origin, state FROM control_commands")
    assert (cmd["kind"], cmd["origin"], cmd["state"]) == ("CANCEL_KNOWN", "KILL_SWITCH", "PENDING")
    assert fingerprint(sql) == before  # no pair, order, ledger or user row moved
    assert sql("SELECT count(*) AS n FROM order_intents")[0]["n"] == 0
    assert sql("SELECT count(*) AS n FROM order_attempts")[0]["n"] == 0
    assert "bot.kill_activated" in events(sql)


def test_a_second_kill_is_refused_and_does_not_queue_a_second_command(
    safe: SafetyEnv, sql: Sql
) -> None:
    assert safe.act("kill").kind == "ok"
    out = safe.act("kill")
    assert out.kind == "not_allowed" and out.reasons == ("ALREADY_ACTIVE",)
    assert sql("SELECT count(*) AS n FROM control_commands")[0]["n"] == 1


def test_the_web_tier_has_no_way_to_release_the_kill_switch(safe: SafetyEnv) -> None:
    safe.act("kill")
    for action in ACTIONS:
        safe.act(action)
    assert safe.control_row().kill_switch == "ACTIVE"


# ------------------------------------------------------------------ cancel known
def test_cancel_known_needs_a_paused_bot_and_queues_one_command(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    out = safe.act("cancel_known")
    assert out.kind == "not_allowed" and out.reasons == ("BOT_MUST_BE_PAUSED",)
    assert safe.act("pause").kind == "ok"
    assert safe.act("cancel_known").kind == "ok"
    (cmd,) = sql("SELECT origin, state FROM control_commands")
    assert (cmd["origin"], cmd["state"]) == ("OPERATOR", "PENDING")
    again = safe.act("cancel_known")
    assert again.kind == "conflict" and again.reasons == ("COMMAND_IN_PROGRESS",)
    assert "bot.cancel_requested" in events(sql)


def test_cancel_known_changes_no_bot_state_by_itself(safe: SafetyEnv, sql: Sql) -> None:
    before = fingerprint(sql)
    assert safe.act("cancel_known").kind == "ok"
    assert fingerprint(sql) == before and safe.control_row().bot_state == "PAUSED"


# ------------------------------------------------------------------ resume
def blockers(safe: SafetyEnv) -> tuple[str, ...]:
    with safe.web_storage.tx() as repos:
        return safe.control.resume_blockers(repos, safe.clock.now())


def test_a_fresh_system_cannot_resume_and_says_why(safe: SafetyEnv, sql: Sql) -> None:
    assert set(blockers(safe)) == {"RECOVERY_INCOMPLETE", "RECONCILIATION_MISSING"}
    out = safe.act("resume")
    assert out.kind == "not_allowed" and "RECOVERY_INCOMPLETE" in out.reasons
    assert safe.control_row().bot_state == "PAUSED"
    denied = sql(
        "SELECT reason_code, detail FROM audit_events WHERE event_code = 'bot.control_denied'"
    )[-1]
    assert denied["reason_code"] in ("RECOVERY_INCOMPLETE", "RECONCILIATION_MISSING")


def test_resume_works_only_after_recovery_and_a_current_reconciliation(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.baseline()
    assert safe.recover().complete
    assert blockers(safe) == ()
    assert safe.act("resume").kind == "ok"
    assert safe.control_row().bot_state == "RUNNING"
    assert events(sql)[-2:] == ["bot.control_requested", "bot.resumed"]
    assert safe.act("resume").reasons == ("ALREADY_RUNNING",)


def test_a_stale_reconciliation_blocks_resume(safe: SafetyEnv) -> None:
    safe.baseline()
    assert safe.recover().complete
    safe.clock.advance(301)
    assert blockers(safe) == ("RECONCILIATION_STALE",)
    safe.clock.advance(
        -301
    )  # the harness clock is ours; the database rule is checked in test_safety_db


def test_a_failed_or_mismatching_reconciliation_blocks_resume(safe: SafetyEnv) -> None:
    safe.baseline()
    assert safe.recover().complete
    safe.fake.fail("list_orders", *[Fault("timeout")] * 3)  # the read is tried three times
    assert safe.recon().outcome == "FAILED"
    assert blockers(safe) == ("RECONCILIATION_FAILED",)
    safe.fake.add_foreign_order()
    assert safe.recon().outcome == "MISMATCH"
    assert blockers(safe) == ("RECONCILIATION_MISMATCH",)
    assert safe.act("resume").kind == "not_allowed"


def test_the_kill_switch_blocks_resume_and_the_host_release_never_resumes(safe: SafetyEnv) -> None:
    safe.running()
    safe.act("kill")
    assert "KILL_SWITCH_ACTIVE" in blockers(safe)
    assert safe.act("resume").kind == "not_allowed"
    refused = safe.host.release_kill("RELEASE KILL SWITCH")  # the queued cancel has not run yet
    assert refused.kind == "not_allowed" and refused.reasons == ("CANCEL_IN_PROGRESS",)
    outcome = safe.commands.run_pending()
    assert outcome is not None and outcome.state == "DONE"
    assert safe.host.release_kill("RELEASE KILL SWITCH").kind == "ok"
    row = safe.control_row()
    assert (row.kill_switch, row.bot_state, row.recovery_state) == (
        "INACTIVE",
        "PAUSED",
        "INCOMPLETE",
    )
    assert "RECOVERY_INCOMPLETE" in blockers(safe)  # recovery must be redone first
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"


def test_an_unknown_order_attempt_blocks_resume(safe: SafetyEnv) -> None:
    safe.running()
    attempt = safe.new_attempt()
    safe.walk(attempt, "SUBMITTING", "UNKNOWN")
    assert safe.act("pause").kind == "ok"
    assert blockers(safe) == ("UNKNOWN_ATTEMPT",)


def test_the_breaker_cooldown_blocks_resume_and_resume_closes_it(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    assert safe.host.trip_breaker("LOSS_LIMIT")
    assert "BREAKER_COOLDOWN" in blockers(safe)
    safe.clock.advance(safe.settings.safety.breaker_cooldown_seconds + 1)
    assert safe.recon().outcome == "OK"
    assert safe.act("resume").kind == "ok"
    row = safe.control_row()
    assert (row.bot_state, row.breaker_state, row.breaker_reason) == ("RUNNING", "CLOSED", None)
    assert (
        sql(
            "SELECT (detail::jsonb->>'breaker_closed') AS c FROM audit_events WHERE event_code = 'bot.resumed' ORDER BY seq"
        )[-1]["c"]
        == "true"
    )


def test_a_reconciliation_older_than_the_trip_does_not_allow_resume(safe: SafetyEnv) -> None:
    safe.running()
    assert safe.recon().outcome == "OK"
    safe.clock.advance(10)
    assert safe.host.trip_breaker("API_FAILURES")
    safe.clock.advance(safe.settings.safety.breaker_cooldown_seconds - 1)
    # cooldown almost over; the OK run predates the trip
    assert "RECONCILIATION_BEFORE_TRIP" in blockers(safe) or "RECONCILIATION_STALE" in blockers(
        safe
    )


# ------------------------------------------------------------------ nothing else moves
@pytest.mark.parametrize("action", ACTIONS)
def test_no_control_action_creates_an_order_or_touches_bot_state_tables(
    safe: SafetyEnv, sql: Sql, action: str
) -> None:
    safe.baseline()
    safe.recover()
    before = fingerprint(sql)
    safe.act(action)
    assert fingerprint(sql) == before
    for table in ("order_intents", "risk_decisions", "order_attempts", "attempt_fills"):
        assert sql(f"SELECT count(*) AS n FROM {table}")[0]["n"] == 0  # noqa: S608
    assert (
        safe.fake.calls == safe.fake.calls[: len(safe.fake.calls)]
        and "submit" not in safe.fake.calls
    )


def test_a_lost_race_is_a_conflict_not_a_silent_overwrite(safe: SafetyEnv) -> None:
    with safe.web_storage.tx() as repos:
        version = repos.safety.control().version
        assert (
            repos.safety.update_control(
                version + 5, safe.clock.now(), bot_state="PAUSED", last_change_reason="TEST_RACE"
            )
            is False
        )


def test_the_overview_reads_state_without_writing_anything(safe: SafetyEnv, sql: Sql) -> None:
    before = (sql("SELECT count(*) AS n FROM audit_events")[0]["n"], fingerprint(sql))
    view = safe.control.overview(safe.ctx)
    assert view.gate.status == "BLOCKED" and view.control.bot_state == "PAUSED"
    assert (sql("SELECT count(*) AS n FROM audit_events")[0]["n"], fingerprint(sql)) == before
