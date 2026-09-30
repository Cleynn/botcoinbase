"""What can be deployed right now, from the funds the venue itself reports.

`exchange_funds` reads the USDC balance through a read-only callable (the private read adapter's
`list_accounts`); the result is what the exchange says is available, already net of the holds on
open orders. `usable_quote` is then

    min(min(available, allocation cap) - protected reserve, max deployment - committed)

where `committed` is quote held by open orders plus the cost of inventory the bot holds. Anything
unreadable, ambiguous, negative or stale yields `FundsUnavailable`, which callers treat as NO_TRADE.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal, Protocol

from app.capital.profiles import ZERO, CapitalProfile

QUOTE: Final = "USDC"
Source = Literal["EXCHANGE", "PAPER"]


class BalanceLike(Protocol):
    """A read-only balance: the exchange adapter's frozen `Balance` satisfies it."""

    @property
    def currency(self) -> str: ...

    @property
    def available(self) -> Decimal: ...

    @property
    def hold(self) -> Decimal: ...


class FundsUnavailable(Exception):
    """The funds could not be established. `code` is a fixed reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Funds:
    available: Decimal  # spendable quote the source reports (net of holds)
    hold: Decimal  # quote locked in open orders at the source
    inventory_cost: Decimal  # cost of base inventory the bot holds
    source: Source
    observed_at: datetime

    @property
    def committed(self) -> Decimal:
        return self.hold + self.inventory_cost


def exchange_funds(
    list_accounts: Callable[[], Iterable[BalanceLike]],
    *,
    inventory_cost: Decimal,
    now: datetime,
    quote: str = QUOTE,
) -> Funds:
    """The venue's own USDC figures. Exactly one account for the quote currency must exist."""
    try:
        accounts = tuple(a for a in list_accounts() if a.currency == quote)
    except Exception as exc:  # any read failure means the funds are unknown, never "zero"
        raise FundsUnavailable("FUNDS_READ_FAILED") from exc
    if not accounts:
        raise FundsUnavailable("NO_QUOTE_ACCOUNT")
    if len(accounts) > 1:
        raise FundsUnavailable("AMBIGUOUS_QUOTE_ACCOUNT")
    account = accounts[0]
    if account.available < ZERO or account.hold < ZERO or inventory_cost < ZERO:
        raise FundsUnavailable("NEGATIVE_BALANCE")
    return Funds(account.available, account.hold, inventory_cost, "EXCHANGE", now)


def paper_funds(
    *, cash: Decimal, reserved: Decimal, inventory_cost: Decimal, now: datetime
) -> Funds:
    """The paper ledger's equivalent: cash, less the quote reserved by open buy orders."""
    if cash < ZERO or reserved < ZERO or inventory_cost < ZERO or reserved > cash:
        raise FundsUnavailable("NEGATIVE_BALANCE")
    return Funds(cash - reserved, reserved, inventory_cost, "PAPER", now)


def simulated_funds(
    *, cash: Decimal, reserved: Decimal, inventory_cost: Decimal, at_epoch_seconds: int
) -> Funds:
    """`paper_funds` stamped with a candle time, for engines that have no clock of their own."""
    return paper_funds(
        cash=cash,
        reserved=reserved,
        inventory_cost=inventory_cost,
        now=datetime.fromtimestamp(at_epoch_seconds, UTC),
    )


def usable_limits(
    funds: Funds, *, allocation_cap: Decimal, reserve: Decimal, max_deployment: Decimal
) -> Decimal:
    """`usable_quote` for explicit limits (a policy may tighten its profile). Never negative."""
    considered = min(funds.available, allocation_cap)
    room = max_deployment - funds.committed
    return max(ZERO, min(considered - reserve, room))


def usable_quote(funds: Funds, profile: CapitalProfile) -> Decimal:
    """What may still be deployed. Never negative; zero means there is nothing to deploy."""
    return usable_limits(
        funds,
        allocation_cap=profile.allocation_cap,
        reserve=profile.protected_reserve,
        max_deployment=profile.max_deployment,
    )


def stale(funds: Funds, now: datetime, max_age_seconds: int) -> bool:
    age = (now - funds.observed_at).total_seconds()
    return age < 0 or age > max_age_seconds
