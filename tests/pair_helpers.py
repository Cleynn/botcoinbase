"""Shared builders for pair tests: validation inputs from the synthetic Coinbase data."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from app.adapters import coinbase_parse as parse
from app.config import FeePolicy, PairPolicy, load_settings
from app.pairs.validation import ValidationInputs
from tests.coinbase_fakes import FakeCoinbase

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
ATTESTED = FeePolicy(operator_maker_rate="0.002", attested_on=date(2026, 9, 20))


def policy(**overrides: Any) -> PairPolicy:
    base = load_settings({"TD_ENVIRONMENT": "test", "TD_SECRET_KEY": "x" * 40}).pair_policy
    return base.model_copy(update={"fees": ATTESTED, **overrides})


def good_inputs(fake: FakeCoinbase | None = None, product_id: str = "BTC-USDC") -> ValidationInputs:
    fake = fake or FakeCoinbase(lambda: NOW)
    with fake.client() as client:
        meta = parse.parse_product_response(client.get_product(product_id))
        server = parse.parse_server_time(client.server_time())
        daily = parse.parse_candles(client.get_candles(product_id, "ONE_DAY", 1, 2 + 86400 * 95))
        intraday = parse.parse_candles(
            client.get_candles(product_id, "FIVE_MINUTE", 1, 2 + 300 * 300)
        )
        book = parse.parse_product_book(client.get_product_book(product_id, 50))
    from app.pairs.policy import derive_data_basis

    return ValidationInputs(
        product=meta,
        verified_at=NOW,
        pair_data_basis=derive_data_basis(meta)[1],
        now=NOW,
        server_time=server,
        daily=daily,
        intraday=intraday,
        book=book,
    )


def with_product(inputs: ValidationInputs, **changes: Any) -> ValidationInputs:
    return replace(inputs, product=replace(inputs.product, **changes))
