"""Display models for the Coinbase page: the account data stored by the host feed.

The web tier only reads the feed tables. It never contacts Coinbase and holds no credential; the
host `feed` service does the reading (GET requests only). Every number is shown as stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.storage.feed_repositories import FeedStatusRow
from app.storage.repositories import Repos
from app.web.view_models import fmt

VENUE = "COINBASE"
STALE_SECONDS = 300  # older than this, the page says the data is stale
MAX_ROWS = 50


@dataclass(frozen=True)
class BalanceLine:
    currency: str
    available: str
    hold: str
    total: str


@dataclass(frozen=True)
class OrderLine:
    created: str
    product: str
    side: str
    status: str
    price: str
    quantity: str
    filled: str
    order_id: str


@dataclass(frozen=True)
class FillLine:
    when: str
    product: str
    side: str
    price: str
    size: str
    fee: str
    liquidity: str


@dataclass(frozen=True)
class ExchangeView:
    state: str  # NEVER | FRESH | STALE | FAILED
    headline: str
    updated: str
    age: str
    permissions: str
    transfer_warning: bool
    balances: tuple[BalanceLine, ...]
    orders: tuple[OrderLine, ...]
    fills: tuple[FillLine, ...]
    totals: tuple[int, int, int]  # stored balances, orders, fills (the tables may be truncated)
    max_rows: int


def num(value: Decimal) -> str:
    """A stored decimal as plain digits: no exponent, no rounding, no trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _age(seconds: int) -> str:
    if seconds < 120:
        return f"{max(seconds, 0)} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def _state(status: FeedStatusRow | None, now: datetime) -> tuple[str, str, int | None]:
    if status is None:
        return (
            "NEVER",
            "No Coinbase data yet. The host feed has not run "
            "(docker compose --profile discovery up -d feed).",
            None,
        )
    if status.succeeded_at is None:
        return "FAILED", f"The feed has never succeeded. Last error: {status.error_code}.", None
    age = int((now - status.succeeded_at).total_seconds())
    if status.state == "FAILED":
        return (
            "FAILED",
            f"The last read failed ({status.error_code}). Showing the data of the last good read.",
            age,
        )
    if age > STALE_SECONDS:
        return "STALE", "The feed has stopped updating. This data is old.", age
    return "FRESH", "Coinbase data is up to date.", age


def _permissions(status: FeedStatusRow | None) -> str:
    if status is None or status.can_view is None:
        return "Not available"
    yes_no = {True: "yes", False: "no", None: "unknown"}
    return (
        f"view {yes_no[status.can_view]}, trade {yes_no[status.can_trade]}, "
        f"transfer {yes_no[status.can_transfer]}"
    )


def build(repos: Repos, now: datetime) -> ExchangeView:
    status = repos.feed.status(VENUE)
    state, headline, age = _state(status, now)
    return ExchangeView(
        state=state,
        headline=headline,
        updated=fmt(status.succeeded_at) if status else "Not available",
        age=_age(age) if age is not None else "Not available",
        permissions=_permissions(status),
        transfer_warning=bool(status and status.can_transfer),
        balances=tuple(
            BalanceLine(b.currency, num(b.available), num(b.hold), num(b.available + b.hold))
            for b in repos.feed.balances(VENUE)
        ),
        orders=tuple(
            OrderLine(
                fmt(o.created_time),
                o.product_id,
                o.side,
                o.status,
                num(o.price),
                num(o.base_qty),
                num(o.filled_qty),
                o.order_id,
            )
            for o in repos.feed.orders(VENUE, MAX_ROWS)
        ),
        fills=tuple(
            FillLine(
                fmt(f.trade_time),
                f.product_id,
                f.side,
                num(f.price),
                num(f.size),
                num(f.fee),
                f.liquidity,
            )
            for f in repos.feed.fills(VENUE, MAX_ROWS)
        ),
        totals=repos.feed.counts(VENUE),
        max_rows=MAX_ROWS,
    )


def feed_rows(repos: Repos, now: datetime) -> tuple[tuple[str, str], ...]:
    """The overview's read-only Coinbase rows (real account data, labelled as such)."""
    status = repos.feed.status(VENUE)
    state, _headline, age = _state(status, now)
    if status is None or status.succeeded_at is None:
        # a real fact, like "None yet" for backtests: the "Known values" tiles never guess
        return (("Coinbase account data (REAL account)", "None yet (the host feed has not run)"),)
    usdc = next((b for b in repos.feed.balances(VENUE) if b.currency == "USDC"), None)
    label = {"FRESH": "updated", "STALE": "STALE, last updated", "FAILED": "read FAILED, last good"}
    return (
        (
            "Coinbase USDC available (REAL account)",
            num(usdc.available) if usdc else "no USDC wallet reported",
        ),
        (
            "Coinbase USDC on hold (REAL account)",
            num(usdc.hold) if usdc else "no USDC wallet reported",
        ),
        ("Coinbase account data", f"{label[state]} {_age(age or 0)}"),
    )
