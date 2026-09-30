"""The order path with fault injection: intent persisted first, risk decision, authorization, the
submit mark committed before I/O, ambiguity leaves UNKNOWN, reconcile before any retry.

The exchange is the scripted FAKE test double (no market, no fills unless injected). Nothing here
touches a network."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest

from app.config import FeePolicy
from app.exchange.errors import ExchangeError
from app.exchange.fake import Fault
from app.exchange.gateway import CancelResult, OrderRequest, SubmitResult
from app.safety.context import BookFacts
from app.safety.pipeline import OrderPipeline, client_order_id, intent_key
from app.safety.types import OrderProposal
from tests.conftest import with_fees
from tests.integration.safety_env import SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]
D = Decimal


def submit_count(safe: SafetyEnv) -> int:
    return safe.fake.calls.count("submit")


def pipeline_with(safe: SafetyEnv, gateway: Any, **changes: Any) -> OrderPipeline:
    return OrderPipeline(
        storage=safe.ctl,
        clock=safe.clock,
        settings=changes.get("settings", safe.settings),
        gateway=gateway,
        boot_id=safe.boot_id,
        reconcile=safe.reconciler.run,
        list_accounts=safe.fake.list_accounts,
    )


class Spy:
    """Wraps the fake gateway: records what the database looked like at the moment of I/O."""

    venue = "FAKE"

    def __init__(
        self, safe: SafetyEnv, *, inspect: Callable[[OrderRequest], None] | None = None
    ) -> None:
        self.safe = safe
        self.inspect = inspect
        self.requests: list[OrderRequest] = []

    def submit(self, request: OrderRequest) -> SubmitResult:
        self.requests.append(request)
        if self.inspect:
            self.inspect(request)
        return self.safe.fake.submit(request)

    def cancel(self, ids: Sequence[str]) -> dict[str, CancelResult]:
        return self.safe.fake.cancel(ids)


class Scripted:
    venue = "FAKE"

    def __init__(
        self,
        safe: SafetyEnv,
        *,
        result: SubmitResult | None = None,
        raises: BaseException | None = None,
        take: bool = False,
    ) -> None:
        self.safe, self.result, self.raises, self.take = safe, result, raises, take

    def submit(self, request: OrderRequest) -> SubmitResult:
        if self.take:
            self.safe.fake.submit(request)  # the exchange took it, then the call went wrong
        if self.raises is not None:
            raise self.raises
        assert self.result is not None
        return self.result

    def cancel(self, ids: Sequence[str]) -> dict[str, CancelResult]:
        return {}


# ------------------------------------------------------------------ the happy path and its order
def test_a_clean_order_goes_through_every_stage_in_order(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    seen: dict[str, Any] = {}

    def inspect(request: OrderRequest) -> None:
        with safe.ctl.tx() as repos:
            attempt = repos.safety.attempt_by_client_id(request.client_order_id)
            assert attempt is not None
            intent = repos.safety.intent(attempt.intent_id)
            seen["state"], seen["mark"] = attempt.state, attempt.submitting_at
            seen["intent"], seen["decisions"] = (
                intent,
                repos.safety.decisions_for(attempt.intent_id),
            )

    spy = Spy(safe, inspect=inspect)
    pipeline = pipeline_with(safe, spy)
    result = pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert result.kind == "submitted" and result.state == "WORKING"
    # at the instant of I/O: the immutable intent existed, an ALLOW was consumed, the mark was committed
    assert seen["intent"] is not None and seen["state"] == "SUBMITTING" and seen["mark"] is not None
    assert [d.decision for d in seen["decisions"]] == ["ALLOW"] and seen["decisions"][
        0
    ].consumed_at is not None
    assert submit_count(safe) == 1 and len(safe.fake.orders) == 1
    codes = [
        r["event_code"]
        for r in sql(
            "SELECT event_code FROM audit_events WHERE event_code LIKE 'order.%' ORDER BY seq"
        )
    ]
    assert codes == [
        "order.intent_created",
        "order.risk_allowed",
        "order.attempt_authorized",
        "order.submitting",
        "order.submitted",
    ]


def test_the_request_on_the_wire_is_exactly_the_intent_as_a_post_only_limit_order(
    safe: SafetyEnv,
) -> None:
    safe.running()
    spy = Spy(safe)
    pipeline_with(safe, spy).submit(
        safe.proposal(price="99.5", qty="0.12"), source="test", slot="s1", book=safe.book()
    )
    (request,) = spy.requests
    assert (request.product_id, request.side, request.price, request.base_qty) == (
        "BTC-USDC",
        "BUY",
        D("99.5"),
        D("0.12"),
    )
    body = request.body()
    assert body["order_configuration"] == {
        "limit_limit_gtc": {"base_size": "0.12", "limit_price": "99.5", "post_only": True}
    }
    order = next(iter(safe.fake.orders.values()))
    assert order.post_only and order.order_type == "limit_limit_gtc"


def test_client_ids_are_deterministic_and_the_same_proposal_is_one_intent(safe: SafetyEnv) -> None:
    safe.running()
    pipeline = safe.pipeline
    first = pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert first.intent_id is not None and first.attempt_id is not None
    attempt = safe.attempt(first.attempt_id)
    assert str(attempt.client_order_id) == str(client_order_id(first.intent_id, 1))
    order = next(iter(safe.fake.orders.values()))
    assert order.client_order_id == str(attempt.client_order_id)
    again = pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert (
        again.intent_id == first.intent_id
        and again.kind == "blocked"
        and "DUPLICATE_INTENT" in again.reasons
    )
    assert submit_count(safe) == 1 and len(safe.fake.orders) == 1
    other = pipeline.submit(safe.proposal(), source="test", slot="s2", book=safe.book())
    assert other.intent_id != first.intent_id  # a new slot is a new intent


def test_the_intent_key_covers_every_field_of_the_proposal(safe: SafetyEnv) -> None:
    base = safe.proposal()
    keys = {intent_key(base, "test", "s1")}
    for change in (
        {"price": D("100.01")}, {"base_qty": D("0.11")}, {"side": "SELL"}, {"venue": "PAPER"}, {"product_id": "ETH-USDC"}, {"pair_id": "other"},
    ):  # fmt: skip
        keys.add(intent_key(replace(base, **change), "test", "s1"))
    keys.add(intent_key(base, "other", "s1"))
    keys.add(intent_key(base, "test", "s2"))
    assert len(keys) == 9 and all(len(k) == 64 for k in keys)


def test_an_intent_is_persisted_even_when_the_decision_blocks_and_never_changes(
    safe: SafetyEnv, sql: Sql
) -> None:
    result = safe.pipeline.submit(
        safe.proposal(), source="test", slot="s1", book=safe.book()
    )  # bot is PAUSED
    assert result.kind == "blocked" and "BOT_NOT_RUNNING" in result.reasons
    (intent,) = sql("SELECT price, base_qty, post_only FROM order_intents")
    assert (intent["price"], intent["post_only"]) == (D("100"), True)
    (decision,) = sql("SELECT decision, reasons FROM risk_decisions")
    assert decision["decision"] == "BLOCK" and "BOT_NOT_RUNNING" in decision["reasons"]
    assert sql("SELECT count(*) AS n FROM order_attempts")[0]["n"] == 0 and submit_count(safe) == 0


def test_with_no_gateway_nothing_is_ever_submitted(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    pipeline = pipeline_with(safe, None)
    result = pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert result.kind == "no_gateway" and result.reasons == ("NO_GATEWAY",)
    assert sql("SELECT count(*) AS n FROM order_intents")[0]["n"] == 1  # the intent is recorded
    assert sql("SELECT count(*) AS n FROM order_attempts")[0]["n"] == 0
    assert "submit" not in safe.fake.calls


# ------------------------------------------------------------------ every block reason on the real path
def setup_block(
    safe: SafetyEnv, name: str
) -> tuple[OrderProposal, BookFacts | None, OrderPipeline | None]:
    proposal, pipeline = safe.proposal(), None
    book: BookFacts | None = safe.book()
    if name == "kill":
        safe.act("kill")
    elif name == "paused":
        safe.act("pause")
    elif name == "breaker":
        safe.host.trip_breaker("API_FAILURES")
    elif name == "stale_reconciliation":
        safe.clock.advance(safe.settings.safety.reconcile_max_age_seconds + 1)
    elif name == "unknown_attempt":
        safe.walk(safe.new_attempt(), "SUBMITTING", "UNKNOWN")
    elif name == "foreign_order":
        safe.fake.add_foreign_order()
        safe.recon()
    elif name == "price_precision":
        proposal = replace(proposal, price=D("100.005"))
    elif name == "size_precision":
        proposal = replace(proposal, base_qty=D("0.100000001"))
    elif name == "min_notional":
        proposal = replace(proposal, price=D("100"), base_qty=D("0.001"))
    elif name == "price_deviation":
        proposal = replace(proposal, price=D("110"))
    elif name == "edge":
        proposal = replace(proposal, expected_cycle_return=D("0.001"))
    elif name == "spread":
        book = safe.book(spread_bps="60")
    elif name == "no_book":
        book = None
    elif name == "stale_book":
        book = safe.book(age=600)
    elif name == "fee_unattested":
        pipeline = pipeline_with(safe, safe.fake, settings=with_fees(safe.settings, FeePolicy()))
    elif name == "production_fake":
        pipeline = pipeline_with(
            safe, safe.fake, settings=safe.settings.model_copy(update={"environment": "production"})
        )
    elif name == "api_failures":
        with safe.ctl.tx() as repos:
            for _ in range(safe.settings.safety.api_failure_threshold):
                repos.safety.add_api_event(
                    "FAKE", "list_orders", False, "TIMEOUT", safe.clock.now()
                )
    return proposal, book, pipeline


BLOCKS = {
    "kill": "KILL_SWITCH_ACTIVE",
    "paused": "BOT_NOT_RUNNING",
    "breaker": "BREAKER_OPEN",
    "stale_reconciliation": "RECONCILIATION_STALE",
    "unknown_attempt": "UNKNOWN_ATTEMPT",
    "foreign_order": "UNKNOWN_ORDER",
    "price_precision": "PRICE_PRECISION",
    "size_precision": "SIZE_PRECISION",
    "min_notional": "BELOW_MIN_NOTIONAL",
    "price_deviation": "PRICE_DEVIATION",
    "edge": "EDGE_BELOW_COSTS",
    "spread": "SPREAD_ABNORMAL",
    "no_book": "SPREAD_UNKNOWN",
    "stale_book": "SPREAD_UNKNOWN",
    "fee_unattested": "FEE_UNATTESTED",
    "production_fake": "LIVE_GATE_BLOCKED",
    "api_failures": "API_FAILURES",
}


@pytest.mark.parametrize("name", sorted(BLOCKS))
def test_every_hot_guard_blocks_before_any_exchange_call(
    safe: SafetyEnv, sql: Sql, name: str
) -> None:
    safe.running()
    proposal, book, pipeline = setup_block(safe, name)
    pipeline = pipeline or safe.pipeline
    submits = submit_count(safe)
    result = pipeline.submit(proposal, source="test", slot=name, book=book)
    assert result.kind == "blocked" and BLOCKS[name] in result.reasons
    assert submit_count(safe) == submits  # never reached the exchange
    assert (
        sql("SELECT count(*) AS n FROM order_attempts WHERE intent_id = %s", (result.intent_id,))[
            0
        ]["n"]
        == 0
    )
    assert (
        sql("SELECT decision FROM risk_decisions WHERE intent_id = %s", (result.intent_id,))[0][
            "decision"
        ]
        == "BLOCK"
    )
    assert "order.risk_blocked" in [
        r["event_code"] for r in sql("SELECT event_code FROM audit_events ORDER BY seq")
    ]


def test_an_intent_over_the_largest_profile_cap_is_refused_by_the_schema_and_never_stored(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    result = safe.pipeline.submit(
        replace(safe.proposal(), base_qty=D("0.6")), source="test", slot="big", book=safe.book()
    )  # 60 USDC: over every profile
    assert result.kind == "invalid" and result.reasons == ("INTENT_REFUSED",)
    assert sql("SELECT count(*) AS n FROM order_intents")[0]["n"] == 0 and submit_count(safe) == 0
    assert "INTENT_REFUSED" in [
        r["reason_code"] for r in sql("SELECT reason_code FROM audit_events")
    ]


def test_an_order_over_the_selected_profiles_cap_is_blocked_and_never_sent(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    result = safe.pipeline.submit(
        replace(safe.proposal(), base_qty=D("0.2")), source="test", slot="big", book=safe.book()
    )  # 20 USDC: fits the table ceiling, not the pilot profile's 12
    assert result.kind == "blocked" and "ORDER_CAP_BREACH" in result.reasons
    assert submit_count(safe) == 0
    assert sql("SELECT count(*) AS n FROM order_attempts")[0]["n"] == 0


def test_stale_market_data_and_metadata_block(safe: SafetyEnv) -> None:
    safe.running()
    safe.clock.advance(4000)  # longer than both freshness limits
    assert safe.recon().outcome == "OK"  # the exchange side is fine; the DATA is old
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="old", book=safe.book())
    assert result.kind == "blocked"
    assert {"STALE_MARKET_DATA", "STALE_METADATA"} <= set(result.reasons)


def test_a_live_venue_cannot_even_be_stored(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    with pytest.raises(psycopg.IntegrityError):
        safe.pipeline.create_intent(replace(safe.proposal(), venue="LIVE"), source="test", slot="x")
    assert sql("SELECT count(*) AS n FROM order_intents")[0]["n"] == 0


def test_the_paper_venue_needs_its_own_baseline_and_the_pipeline_refuses_without_one(
    safe: SafetyEnv,
) -> None:
    safe.running()
    result = safe.pipeline.submit(
        replace(safe.proposal(), venue="PAPER"), source="test", slot="p", book=safe.book()
    )
    assert result.kind == "blocked"
    assert {"RESERVE_BREACH", "EQUITY_UNKNOWN"} <= set(result.reasons)


# ------------------------------------------------------------------ faults on the submit call
NOT_TAKEN = [
    Fault("timeout"), Fault("network"), Fault("server_error"), Fault("rate_limited"),
]  # fmt: skip


@pytest.mark.parametrize("fault", NOT_TAKEN, ids=lambda f: f.kind)
def test_an_ambiguous_failure_before_processing_leaves_unknown_and_never_resubmits(
    safe: SafetyEnv, sql: Sql, fault: Fault
) -> None:
    safe.running()
    safe.fake.fail("submit", fault)
    first = safe.pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert first.kind == "unknown" and first.state == "UNKNOWN"
    assert safe.fake.orders == {}  # the exchange never took it
    assert "list_orders" in safe.fake.calls  # reconciled straight away (after the ambiguity)
    # trying again is blocked: an UNKNOWN attempt blocks everything until reconciliation resolves it
    again = safe.pipeline.process(first.intent_id, book=safe.book())  # type: ignore[arg-type]
    assert again.kind == "blocked" and "UNKNOWN_ATTEMPT" in again.reasons
    assert submit_count(safe) == 1  # NO blind retry
    codes = [
        r["event_code"]
        for r in sql(
            "SELECT event_code FROM audit_events WHERE event_code LIKE 'order.%' ORDER BY seq"
        )
    ]
    assert "order.unknown" in codes and codes.count("order.submitting") == 1


@pytest.mark.parametrize(
    "fault",
    [
        Fault("timeout", processed=True),
        Fault("network", processed=True),
        Fault("server_error", processed=True),
    ],
    ids=lambda f: f.kind,
)
def test_an_ambiguous_failure_after_processing_is_adopted_by_reconciliation(
    safe: SafetyEnv, sql: Sql, fault: Fault
) -> None:
    safe.running()
    safe.fake.fail("submit", fault)
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert result.kind == "unknown"
    assert result.state == "WORKING"  # reconciliation found the order and adopted it
    assert len(safe.fake.orders) == 1 and submit_count(safe) == 1
    order = next(iter(safe.fake.orders.values()))
    attempt = safe.attempt(result.attempt_id)  # type: ignore[arg-type]
    assert (
        attempt.exchange_order_id == order.order_id
        and str(attempt.client_order_id) == order.client_order_id
    )
    events = [
        r["event_code"]
        for r in sql(
            "SELECT event_code FROM audit_events WHERE event_code LIKE 'order.%' ORDER BY seq"
        )
    ]
    assert events.index("order.unknown") < events.index("order.resolved")


def test_after_a_proven_absence_a_retry_is_a_new_attempt_with_a_new_client_id(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    safe.fake.fail("submit", Fault("timeout"))
    first = safe.pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert first.kind == "unknown"
    # too early: absence is not proven, so a retry is refused (and reconciles first)
    early = safe.pipeline.retry(first.intent_id, book=safe.book())  # type: ignore[arg-type]
    assert early.kind == "blocked" and submit_count(safe) == 1
    submitted = safe.attempt(first.attempt_id).submitting_at  # type: ignore[arg-type]
    safe.clock.advance(max((submitted - safe.clock.now()).total_seconds() + 125, 0))
    assert safe.recon().outcome == "OK"
    safe.clock.advance(61)
    assert safe.recon().outcome == "OK"
    assert safe.attempt(first.attempt_id).state == "ABSENT"  # type: ignore[arg-type]
    assert "order.absent" in [r["event_code"] for r in sql("SELECT event_code FROM audit_events")]
    safe.clock.advance(1)
    second = safe.pipeline.retry(first.intent_id, book=safe.book())  # type: ignore[arg-type]
    assert second.kind == "submitted"
    a1, a2 = safe.attempt(first.attempt_id), safe.attempt(second.attempt_id)  # type: ignore[arg-type]
    assert a2.attempt_no == 2 and a2.client_order_id != a1.client_order_id
    assert len(safe.fake.orders) == 1 and submit_count(safe) == 2  # exactly one live order


def test_a_definite_rejection_is_recorded_and_a_retry_is_a_fresh_attempt(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.running()
    safe.fake.fail("submit", Fault("reject", reason="INVALID_LIMIT_PRICE_POST_ONLY"))
    first = safe.pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    assert first.kind == "rejected" and first.reasons == ("INVALID_LIMIT_PRICE_POST_ONLY",)
    attempt = safe.attempt(first.attempt_id)  # type: ignore[arg-type]
    assert (attempt.state, attempt.failure_code) == ("REJECTED", "INVALID_LIMIT_PRICE_POST_ONLY")
    assert safe.fake.orders == {}
    retry = safe.pipeline.retry(first.intent_id, book=safe.book())  # type: ignore[arg-type]
    assert retry.kind == "submitted" and safe.attempt(retry.attempt_id).attempt_no == 2  # type: ignore[arg-type]
    assert "order.rejected" in [r["event_code"] for r in sql("SELECT event_code FROM audit_events")]


def test_a_returned_existing_order_is_adopted_and_reconciled(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    intent = safe.pipeline.create_intent(safe.proposal(), source="test", slot="dup")
    cid = client_order_id(intent.id, 1)
    safe.fake.submit(OrderRequest(str(cid), "BTC-USDC", "BUY", intent.price, intent.base_qty))
    result = safe.pipeline.process(intent.id, book=safe.book())
    assert result.kind == "adopted" and result.state == "WORKING"
    assert len(safe.fake.orders) == 1
    assert "list_orders" in safe.fake.calls  # DUPLICATE_CLIENT_ORDER_ID triggers reconciliation


def test_an_existing_order_that_differs_from_the_intent_is_rejected_and_flagged(
    safe: SafetyEnv,
) -> None:
    safe.running()
    intent = safe.pipeline.create_intent(safe.proposal(), source="test", slot="dup2")
    cid = client_order_id(intent.id, 1)
    safe.fake.submit(OrderRequest(str(cid), "BTC-USDC", "BUY", D("101"), intent.base_qty))
    result = safe.pipeline.process(intent.id, book=safe.book())
    assert result.kind == "rejected" and result.reasons == ("DUPLICATE_CLIENT_ORDER_ID_MISMATCH",)
    run = safe.recon()  # an order wearing our client id exists although the attempt was rejected
    assert run.outcome == "MISMATCH" and "ORDER_MISMATCH" in run.codes


@pytest.mark.parametrize(
    "raises", [RuntimeError("boom"), ValueError("x"), ExchangeError("UNEXPECTED_RESPONSE")]
)
def test_an_unexpected_error_after_the_mark_is_ambiguous(
    safe: SafetyEnv, raises: Exception
) -> None:
    safe.running()
    result = pipeline_with(safe, Scripted(safe, raises=raises)).submit(
        safe.proposal(), source="test", slot="s1", book=safe.book()
    )
    assert result.kind == "unknown" and result.state == "UNKNOWN"


@pytest.mark.parametrize(
    "weird",
    [SubmitResult("ACCEPTED", None), SubmitResult("UNKNOWN"), SubmitResult("EXISTING", None)],
)
def test_a_success_without_an_exchange_id_is_never_taken_as_success(
    safe: SafetyEnv, weird: SubmitResult
) -> None:
    safe.running()
    result = pipeline_with(safe, Scripted(safe, result=weird)).submit(
        safe.proposal(), source="test", slot="s1", book=safe.book()
    )
    assert result.kind == "unknown"


def test_a_crash_after_the_submit_mark_is_recovered_by_reconciliation(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.baseline()
    assert safe.recover().complete and safe.act("resume").kind == "ok"
    with pytest.raises(SystemExit):
        pipeline_with(safe, Scripted(safe, raises=SystemExit(1), take=True)).submit(
            safe.proposal(), source="test", slot="s1", book=safe.book()
        )
    (row,) = sql("SELECT id, state FROM order_attempts")
    assert row["state"] == "SUBMITTING"  # the process died between the mark and the answer
    recovered = safe.recover()  # a new boot: reconcile before doing anything
    assert sql("SELECT state, exchange_order_id FROM order_attempts")[0]["state"] == "WORKING"
    assert recovered.complete and len(safe.fake.orders) == 1
    assert safe.control_row().bot_state == "PAUSED"  # recovery never resumes


def test_a_crash_after_the_mark_when_the_exchange_never_took_it_stays_unknown(
    safe: SafetyEnv, sql: Sql
) -> None:
    safe.baseline()
    assert safe.recover().complete and safe.act("resume").kind == "ok"
    with pytest.raises(SystemExit):
        pipeline_with(safe, Scripted(safe, raises=SystemExit(1), take=False)).submit(
            safe.proposal(), source="test", slot="s1", book=safe.book()
        )
    recovered = safe.recover()
    assert sql("SELECT state FROM order_attempts")[0]["state"] == "UNKNOWN"
    assert not recovered.complete and "UNKNOWN_ATTEMPT" in recovered.blockers
    assert safe.fake.orders == {}


def test_a_crash_before_the_mark_is_closed_as_not_sent(safe: SafetyEnv, sql: Sql) -> None:
    safe.baseline()
    assert safe.recover().complete and safe.act("resume").kind == "ok"
    intent = safe.pipeline.create_intent(safe.proposal(), source="test", slot="s1")
    authorized = safe.pipeline._authorize(intent.id, safe.book())
    assert not hasattr(
        authorized, "kind"
    )  # an (attempt id, request) pair: authorized, never marked
    assert sql("SELECT state FROM order_attempts")[0]["state"] == "AUTHORIZED"
    assert safe.recover().complete
    (row,) = sql("SELECT state, failure_code FROM order_attempts")
    assert (row["state"], row["failure_code"]) == (
        "REJECTED",
        "NOT_SENT",
    ) and safe.fake.orders == {}


def test_a_refusal_by_the_database_guard_after_the_decision_blocks_the_order(
    safe: SafetyEnv, sql: Sql
) -> None:
    """A race: the kill switch flips between the risk decision and the authorization insert."""
    safe.running()
    real = safe.pipeline._builder.build
    calls = {"n": 0}

    def flip(*args: Any, **kwargs: Any) -> Any:
        facts = real(*args, **kwargs)
        calls["n"] += 1
        safe.host.activate_kill(
            "ACTIVATE KILL SWITCH"
        )  # host flips it right after the facts were read
        return facts

    safe.pipeline._builder.build = flip  # type: ignore[method-assign]
    result = safe.pipeline.submit(safe.proposal(), source="test", slot="race", book=safe.book())
    assert result.kind == "blocked" and result.reasons == ("INPUTS_UNAVAILABLE",)
    assert submit_count(safe) == 0 and sql("SELECT count(*) AS n FROM order_attempts")[0]["n"] == 0
    assert "AUTHORIZATION_REFUSED" in [
        r["reason_code"] for r in sql("SELECT reason_code FROM audit_events")
    ]


def test_the_decision_is_recomputed_for_every_attempt(safe: SafetyEnv, sql: Sql) -> None:
    safe.running()
    safe.fake.fail("submit", Fault("reject"))
    first = safe.pipeline.submit(safe.proposal(), source="test", slot="s1", book=safe.book())
    safe.pipeline.retry(first.intent_id, book=safe.book())  # type: ignore[arg-type]
    decisions = sql(
        "SELECT decision, consumed_at IS NOT NULL AS used FROM risk_decisions ORDER BY decided_at"
    )
    assert len(decisions) == 2 and all(d["decision"] == "ALLOW" and d["used"] for d in decisions)


def test_no_more_than_five_attempts_per_intent(safe: SafetyEnv) -> None:
    safe.running()
    intent = safe.pipeline.create_intent(safe.proposal(), source="test", slot="many")
    for _ in range(5):
        safe.fake.fail("submit", Fault("reject"))
        assert safe.pipeline.process(intent.id, book=safe.book()).kind == "rejected"
    safe.fake.fail("submit", Fault("reject"))
    with pytest.raises(psycopg.errors.Error):
        safe.pipeline.process(intent.id, book=safe.book())  # the schema caps attempts at five


def test_a_stale_intent_uuid_is_invalid_not_an_error(safe: SafetyEnv) -> None:
    safe.running()
    assert safe.pipeline.process(UUID(int=7), book=safe.book()).kind == "invalid"
