"""Pair policy: which products are candidates, and how their data basis is derived.

Pure functions over parsed metadata. The default policy admits Coinbase SPOT products quoted in
USDC whose status and flags say they can trade (CB-3), excluding stable-coin bases (CI-11).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.domain.pairs import ProductMetadata

SPOT: Final = "SPOT"
VENUE: Final = "CBE"
QUOTE: Final = "USDC"
# The `status` enumeration is undocumented (CF-10): only exact known-good values pass.
ALLOWED_STATUSES: Final = frozenset({"online"})
# Flags that make a product unusable for a post-only limit grid. `limit_only` is fine (we only
# place limit orders); `post_only=true` means orders cannot be cancelled, so it fails (CB-3).
BLOCKING_FLAGS: Final = (
    "is_disabled",
    "trading_disabled",
    "cancel_only",
    "auction_mode",
    "post_only",
)
ALL_FLAGS: Final = (*BLOCKING_FLAGS, "limit_only")
# Stable-coin and fiat-pegged bases: a grid on a pegged pair has no range to trade (CI-11).
STABLE_BASES: Final = frozenset(
    {
        "USDT",
        "USDC",
        "DAI",
        "EURC",
        "PYUSD",
        "USDS",
        "USDP",
        "TUSD",
        "GUSD",
        "FDUSD",
        "BUSD",
        "LUSD",
        "EURT",
        "GYEN",
        "PAX",
        "UST",
        "USDE",
        "RLUSD",
        "USD1",
        "FRAX",
    }
)
# Quote-currency products whose market data is NOT the matching -USD book (CF-11).
OWN_BOOK_PRODUCTS: Final = frozenset({"USDT-USDC", "EURC-USDC"})
DATA_QUOTES: Final = ("USD", "USDC")


@dataclass(frozen=True)
class ProductVerdict:
    failures: tuple[str, ...]  # reason codes: definitely unusable
    inconclusive: tuple[str, ...]  # reason codes: cannot tell

    @property
    def ok(self) -> bool:
        return not self.failures and not self.inconclusive


def assess_product(meta: ProductMetadata) -> ProductVerdict:
    """PRODUCT_OK (CB-3) on static metadata. Freshness is judged separately."""
    failures: list[str] = []
    unknown: list[str] = []
    if meta.product_type != SPOT:
        failures.append("NOT_SPOT")
    if meta.venue != VENUE:
        failures.append("WRONG_VENUE")
    if meta.quote_currency != QUOTE:
        failures.append("QUOTE_NOT_USDC")
    if "status" not in meta.malformed and meta.status not in ALLOWED_STATUSES:
        failures.append("STATUS_NOT_ALLOWED")
    for flag in BLOCKING_FLAGS:
        value = getattr(meta, flag)
        if value is True:
            failures.append(f"FLAG_{flag.upper()}")
        elif value is None:
            unknown.append(f"FLAG_{flag.upper()}_MISSING")
    if "status" in meta.malformed:
        unknown.append("STATUS_UNPARSEABLE")
    return ProductVerdict(tuple(failures), tuple(unknown))


def is_stable_base(meta: ProductMetadata) -> bool:
    return meta.base_currency in STABLE_BASES


def is_default_candidate(meta: ProductMetadata) -> bool:
    """Active USDC spot product that is not a stable-base pair (the default candidate set)."""
    return assess_product(meta).ok and not is_stable_base(meta)


def derive_data_basis(meta: ProductMetadata) -> tuple[str | None, str]:
    """(data product id, data basis) from the alias fields (CB-4).

    A `-USD` alias means the -USDC market data is the unified USD book. An explicit self-alias or
    the two documented exceptions mean the product has its own book. Anything else is UNKNOWN and
    validation reports INCONCLUSIVE rather than assuming.
    """
    alias = meta.alias
    if alias:
        base, _, quote = alias.rpartition("-")
        if base == meta.base_currency and quote in DATA_QUOTES:
            if quote == "USD":
                return alias, "UNIFIED_USD_BOOK"
            if alias == meta.product_id:
                return alias, "OWN_BOOK"
        return None, "UNKNOWN"
    if meta.product_id in OWN_BOOK_PRODUCTS:
        return meta.product_id, "OWN_BOOK"
    return None, "UNKNOWN"
