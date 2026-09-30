"""Capital profiles, exchange/paper funds and the usable-quote formula. Decimal only; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app import constants
from app.capital.funds import (
    Funds,
    FundsUnavailable,
    exchange_funds,
    paper_funds,
    stale,
    usable_quote,
)
from app.capital.profiles import (
    ABSOLUTE_MAX_DEPLOYMENT,
    ABSOLUTE_MAX_ORDER,
    DEFAULT_PROFILE,
    PROFILES,
    CapitalProfile,
    get_profile,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PILOT = get_profile("pilot")


@dataclass(frozen=True)
class Acct:
    currency: str
    available: D
    hold: D = D(0)


def funds(available: str, hold: str = "0", inventory: str = "0") -> Funds:
    return Funds(D(available), D(hold), D(inventory), "EXCHANGE", NOW)


# ------------------------------------------------------------------ profiles
def test_the_default_profile_equals_the_original_hard_ceilings() -> None:
    p = get_profile(DEFAULT_PROFILE)
    assert (p.allocation_cap, p.protected_reserve, p.max_deployment, p.max_order) == (
        constants.POLICY_TOTAL_CAPITAL,
        constants.POLICY_MIN_RESERVE,
        constants.POLICY_MAX_DEPLOYMENT,
        constants.POLICY_MAX_ORDER_NOTIONAL,
    )


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_every_registered_profile_is_internally_consistent(name: str) -> None:
    p = PROFILES[name]
    assert p.name == name
    assert p.protected_reserve + p.max_deployment <= p.allocation_cap
    assert p.max_order <= p.max_deployment <= ABSOLUTE_MAX_DEPLOYMENT
    assert p.max_order <= ABSOLUTE_MAX_ORDER
    assert p.protected_reserve >= p.allocation_cap * D("0.20")


def test_the_documented_profiles_have_the_documented_numbers() -> None:
    rows = {
        n: (p.allocation_cap, p.protected_reserve, p.max_deployment) for n, p in PROFILES.items()
    }
    assert rows["expanded"] == (D(100), D(25), D(75))
    assert rows["medium"] == (D(250), D(50), D(150))
    assert rows["research"] == (D(0), D(0), D(0))
    assert not PROFILES["research"].trades


@pytest.mark.parametrize(
    "args",
    [
        ("Bad Name", "t", "50", "15", "35", "12"),
        ("x", "t", "50", "15", "35", "12"),
        ("ok", "t", "50", "15", "40", "12"),  # reserve + deployment over the cap
        ("ok", "t", "50", "15", "35", "36"),  # an order above the deployment cap
        ("ok", "t", "50", "5", "35", "12"),  # reserve below 20% of the cap
        ("ok", "t", "-1", "0", "0", "0"),
    ],
)
def test_an_inconsistent_profile_cannot_be_built(args: tuple[str, ...]) -> None:
    name, title, *amounts = args
    with pytest.raises(ValueError):
        CapitalProfile(name, title, *(D(a) for a in amounts))


def test_an_unknown_profile_name_never_falls_back() -> None:
    with pytest.raises(KeyError):
        get_profile("huge")


def test_a_float_amount_is_not_a_profile() -> None:
    with pytest.raises(ValueError):
        CapitalProfile("ok", "t", 50.0, D(15), D(35), D(12))  # type: ignore[arg-type]


# ------------------------------------------------------------------ usable quote
@pytest.mark.parametrize(
    ("profile", "available", "hold", "inventory", "expected"),
    [
        ("pilot", "50", "0", "0", "35"),  # the starter example: 50 - 15 reserve, cap 35
        ("pilot", "200", "0", "0", "35"),  # extra deposits never widen the exposure
        ("pilot", "30", "0", "0", "15"),  # short balance: available - reserve
        ("pilot", "15", "0", "0", "0"),  # only the reserve is left
        ("pilot", "10", "0", "0", "0"),  # below the reserve never goes negative
        ("pilot", "50", "10", "0", "25"),  # quote locked in open orders counts against room
        ("pilot", "50", "0", "20", "15"),  # inventory counts against the deployment room
        ("pilot", "50", "20", "20", "0"),  # deployment fully used
        ("pilot", "50", "30", "30", "0"),  # more committed than allowed: never negative
        ("expanded", "100", "0", "0", "75"),
        ("medium", "250", "0", "0", "150"),
        ("research", "500", "0", "0", "0"),
    ],
)
def test_usable_quote(
    profile: str, available: str, hold: str, inventory: str, expected: str
) -> None:
    assert usable_quote(funds(available, hold, inventory), get_profile(profile)) == D(expected)


def test_the_allocation_cap_limits_what_is_considered() -> None:
    wide = CapitalProfile("wide", "w", D(100), D(25), D(75), D(25))
    assert usable_quote(funds("10000"), wide) == D(75)
    assert usable_quote(funds("60"), wide) == D(35)


def test_the_result_never_exceeds_either_limit() -> None:
    for p in PROFILES.values():
        for avail in ("0", "1", "14.99", "15", "49.99", "50", "99", "100", "1000"):
            for hold in ("0", "5", "40"):
                got = usable_quote(funds(avail, hold), p)
                assert D(0) <= got <= p.max_deployment
                assert got <= max(D(0), D(avail) - p.protected_reserve)


# ------------------------------------------------------------------ exchange funds
def test_exchange_funds_reads_the_usdc_account_only() -> None:
    accounts = [Acct("BTC", D("1")), Acct("USDC", D("80.5"), D("4.5")), Acct("ETH", D("2"))]
    got = exchange_funds(lambda: accounts, inventory_cost=D("3"), now=NOW)
    assert (got.available, got.hold, got.inventory_cost, got.source) == (
        D("80.5"), D("4.5"), D("3"), "EXCHANGE",
    )  # fmt: skip
    assert got.committed == D("7.5")


@pytest.mark.parametrize(
    ("accounts", "code"),
    [
        ([], "NO_QUOTE_ACCOUNT"),
        ([Acct("BTC", D(1))], "NO_QUOTE_ACCOUNT"),
        ([Acct("USDC", D(1)), Acct("USDC", D(2))], "AMBIGUOUS_QUOTE_ACCOUNT"),
        ([Acct("USDC", D(-1))], "NEGATIVE_BALANCE"),
        ([Acct("USDC", D(1), D(-1))], "NEGATIVE_BALANCE"),
    ],
)
def test_unclear_exchange_funds_are_unavailable(accounts: list[Acct], code: str) -> None:
    with pytest.raises(FundsUnavailable) as info:
        exchange_funds(lambda: accounts, inventory_cost=D(0), now=NOW)
    assert info.value.code == code


def test_a_failed_read_is_unknown_funds_never_zero() -> None:
    def boom() -> list[Acct]:
        raise TimeoutError("read timed out")

    with pytest.raises(FundsUnavailable) as info:
        exchange_funds(boom, inventory_cost=D(0), now=NOW)
    assert info.value.code == "FUNDS_READ_FAILED"


def test_negative_inventory_is_refused() -> None:
    with pytest.raises(FundsUnavailable):
        exchange_funds(lambda: [Acct("USDC", D(1))], inventory_cost=D(-1), now=NOW)


def test_paper_funds_are_cash_less_reserved_buys() -> None:
    got = paper_funds(cash=D("40"), reserved=D("10"), inventory_cost=D("5"), now=NOW)
    assert (got.available, got.hold, got.source) == (D("30"), D("10"), "PAPER")
    with pytest.raises(FundsUnavailable):
        paper_funds(cash=D("5"), reserved=D("10"), inventory_cost=D(0), now=NOW)


def test_funds_go_stale() -> None:
    f = funds("50")
    assert not stale(f, NOW + timedelta(seconds=30), 60)
    assert stale(f, NOW + timedelta(seconds=61), 60)
    assert stale(f, NOW - timedelta(seconds=1), 60)  # from the future: never fresh
