"""Strict parsing of (synthetic) Coinbase public responses. Everything external is untrusted."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.adapters import coinbase_parse as p
from tests.coinbase_fakes import book_json, daily_candles, product_json


def body(obj: Any) -> bytes:
    return json.dumps(obj).encode()


# ------------------------------------------------------------------ numbers
@pytest.mark.parametrize("value", ["1", "0.01", "0.00000001", "123456.789", 5])
def test_decimals_accept_plain_numbers(value: Any) -> None:
    assert p.parse_decimal(value) == Decimal(str(value))


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-1",
        "+1",
        "1e5",
        "1E-8",
        "NaN",
        "Infinity",
        "-Infinity",
        "1,5",
        " 1",
        "1 ",
        "0x10",
        "١٢٣",
        "1." + "0" * 40,
        "9" * 40,
        None,
        True,
        False,
        [],
        {},
        1.5,
    ],
)
def test_decimals_refuse_everything_else_including_floats(value: Any) -> None:
    assert p.parse_decimal(value) is None


def test_json_floats_are_read_as_decimal_never_float() -> None:
    data = p.load_json(b'{"x": 0.1}')
    assert isinstance(data["x"], Decimal) and data["x"] == Decimal("0.1")


@pytest.mark.parametrize("raw", [b"NaN", b"Infinity", b"-Infinity", b'{"a": NaN}'])
def test_json_constants_are_refused(raw: bytes) -> None:
    with pytest.raises(p.ParseError):
        p.load_json(raw)


def test_invalid_and_deeply_nested_json_is_a_parse_error_not_a_crash() -> None:
    for raw in (b"", b"{", b"[1,", b"\xff\xfe", b"[" * 100000 + b"]" * 100000):
        with pytest.raises(p.ParseError):
            p.load_json(raw)


def test_canonical_decimal_text_ignores_trailing_zeros() -> None:
    assert p.decimal_text(Decimal("0.0100")) == "0.01"
    assert p.decimal_text(Decimal("100")) == "100"
    assert p.decimal_text(Decimal("1E+2")) == "100"
    assert p.decimal_text(None) is None


# ------------------------------------------------------------------ products
def test_a_normal_product_parses_with_every_field() -> None:
    meta = p.parse_product(product_json("BTC-USDC"))
    assert meta is not None
    assert (meta.base_currency, meta.quote_currency, meta.product_type, meta.venue) == (
        "BTC",
        "USDC",
        "SPOT",
        "CBE",
    )
    assert meta.base_increment == Decimal("0.00000001") and meta.price_increment == Decimal("0.01")
    assert meta.alias == "BTC-USD" and meta.malformed == ()
    assert meta.is_disabled is False and meta.post_only is False


@pytest.mark.parametrize(
    "product_id",
    [
        "BTC-USD",
        "btc-usdc",
        "BTC-USDC ",
        "BTC_USDC",
        "BTC-USDC\n",
        "",
        "-USDC",
        "A" * 30 + "-USDC",
        "BTC-USDC-PERP",
        "<b>-USDC",
        "BTC-USDC;DROP",
        "../BTC-USDC",
        "BTC-USDC/x",
    ],
)
def test_untrustworthy_identities_are_dropped(product_id: str) -> None:
    assert p.parse_product({**product_json("BTC-USDC"), "product_id": product_id}) is None


def test_currency_ids_must_agree_with_the_product_id() -> None:
    assert p.parse_product(product_json("BTC-USDC", base_currency_id="ETH")) is None
    assert p.parse_product(product_json("BTC-USDC", quote_currency_id="USD")) is None


def test_non_object_entries_are_ignored() -> None:
    for junk in (None, 1, "x", [], [product_json()]):
        assert p.parse_product(junk) is None


def test_hostile_status_becomes_a_sentinel_and_is_flagged() -> None:
    for hostile in ("<script>alert(1)</script>", "online\x00", "x" * 200, "", None, 5, "on line"):
        meta = p.parse_product(product_json("BTC-USDC", status=hostile))
        assert meta is not None
        assert meta.status == p.STATUS_SENTINEL and "status" in meta.malformed


def test_unparseable_flags_and_numbers_are_recorded_by_name() -> None:
    meta = p.parse_product(
        product_json(
            "BTC-USDC",
            is_disabled="no",
            base_increment="-1",
            quote_increment="0",
            price_increment="1e-2",
            base_min_size="abc",
        )
    )
    assert meta is not None
    assert meta.is_disabled is None
    assert meta.base_increment is None and meta.quote_increment is None
    assert meta.price_increment is None and meta.base_min_size is None
    assert {
        "is_disabled",
        "base_increment",
        "quote_increment",
        "price_increment",
        "base_min_size",
    } <= set(meta.malformed)


def test_missing_required_numbers_are_flagged_but_optional_ones_are_not() -> None:
    raw = product_json("BTC-USDC")
    for key in (
        "base_increment",
        "quote_increment",
        "base_min_size",
        "price_increment",
        "base_max_size",
        "quote_min_size",
        "quote_max_size",
    ):
        raw.pop(key)
    meta = p.parse_product(raw)
    assert meta is not None
    assert set(meta.malformed) == {"base_increment", "quote_increment", "base_min_size"}
    assert meta.price_increment is None and meta.quote_max_size is None


def test_alias_fields_are_validated_and_display_names_are_never_kept() -> None:
    meta = p.parse_product(
        product_json(
            "BTC-USDC", alias="<img src=x onerror=alert(1)>", alias_to=["ETH-USD", "<x>", 5]
        )
    )
    assert meta is not None
    assert meta.alias is None and meta.alias_to == ("ETH-USD",)
    assert {"alias", "alias_to"} <= set(meta.malformed)
    assert not hasattr(meta, "display_name") and not hasattr(meta, "base_name")
    assert "Synthetic" not in repr(meta)


def test_product_list_counts_seen_entries_and_drops_non_usdc() -> None:
    raw = [product_json("BTC-USDC"), product_json("BTC-USD"), {"junk": 1}, "x"]
    parsed = p.parse_product_list(body({"products": raw}))
    assert parsed.seen == 4
    assert [x.metadata.product_id for x in parsed.listings] == ["BTC-USDC"]


def test_product_list_volume_is_used_for_ordering_only() -> None:
    parsed = p.parse_product_list(
        body({"products": [product_json("BTC-USDC", approximate_quote_24h_volume="abc")]})
    )
    assert parsed.listings[0].volume_key == 0


@pytest.mark.parametrize("raw", [{}, {"products": None}, {"products": {}}, [], "x", 5])
def test_product_list_shape_errors(raw: Any) -> None:
    with pytest.raises(p.ParseError):
        p.parse_product_list(body(raw))


def test_metadata_hash_changes_only_with_content() -> None:
    a = p.parse_product(product_json("BTC-USDC"))
    same = p.parse_product(
        product_json("BTC-USDC", price="61000", approximate_quote_24h_volume="1")
    )
    changed = p.parse_product(product_json("BTC-USDC", trading_disabled=True))
    other_text = p.parse_product(product_json("BTC-USDC", base_increment="0.0000010"))
    assert a and same and changed and other_text
    assert p.metadata_sha256(a) == p.metadata_sha256(same)  # price and volume are not metadata
    assert p.metadata_sha256(a) != p.metadata_sha256(changed)
    assert p.metadata_sha256(a) != p.metadata_sha256(other_text)
    assert len(p.metadata_sha256(a)) == 64


def test_metadata_hash_is_insensitive_to_number_formatting() -> None:
    a = p.parse_product(product_json("BTC-USDC", base_increment="0.01"))
    b = p.parse_product(product_json("BTC-USDC", base_increment="0.0100"))
    assert a and b and p.metadata_sha256(a) == p.metadata_sha256(b)


# ------------------------------------------------------------------ time
def test_server_time_accepts_the_documented_shape() -> None:
    t = p.parse_server_time(
        body({"iso": "x", "epochSeconds": "1790000000", "epochMillis": "1790000000123"})
    )
    assert t.epoch_seconds == 1790000000 and t.epoch_millis == 1790000000123


@pytest.mark.parametrize("value", ["", "abc", "-5", "1", "99999999999999", None, True, 12.5])
def test_server_time_rejects_nonsense(value: Any) -> None:
    with pytest.raises(p.ParseError):
        p.parse_server_time(body({"epochSeconds": value}))


# ------------------------------------------------------------------ candles
def test_candles_are_sorted_ascending_and_typed() -> None:
    raw = daily_candles(1790000000, days=5)
    parsed = p.parse_candles(body({"candles": list(reversed(raw))}))
    assert [c.start for c in parsed.candles] == sorted(c.start for c in parsed.candles)
    assert isinstance(parsed.candles[0].high, Decimal) and parsed.malformed == 0


def test_malformed_candles_are_counted_not_guessed() -> None:
    good = daily_candles(1790000000, days=2)
    bad = [
        {"start": "x", "low": "1", "high": "2", "open": "1", "close": "2", "volume": "1"},
        {"start": "1", "low": "-1", "high": "2", "open": "1", "close": "2", "volume": "1"},
        {"start": "1"},
        "junk",
        None,
    ]
    parsed = p.parse_candles(body({"candles": good + bad}))
    assert len(parsed.candles) == 2 and parsed.malformed == 5


def test_more_than_350_candles_are_truncated_and_reported() -> None:
    many = daily_candles(1790000000, days=340) * 2
    parsed = p.parse_candles(body({"candles": many}))
    assert len(parsed.candles) <= 350 and parsed.malformed == len(many) - 350


@pytest.mark.parametrize("raw", [{}, {"candles": None}, {"candles": "x"}, []])
def test_candle_shape_errors(raw: Any) -> None:
    with pytest.raises(p.ParseError):
        p.parse_candles(body(raw))


# ------------------------------------------------------------------ order book
def test_book_is_sorted_best_first_and_timestamped() -> None:
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    book = p.parse_product_book(body(book_json(now)))
    assert book.bids[0].price > book.bids[-1].price and book.asks[0].price < book.asks[-1].price
    assert book.time == now and book.malformed == 0


def test_book_drops_bad_levels_and_bad_times() -> None:
    raw = {
        "pricebook": {
            "bids": [
                {"price": "0", "size": "1"},
                {"price": "5", "size": "x"},
                3,
                {"price": "10", "size": "1"},
            ],
            "asks": [{"price": "11", "size": "1"}],
            "time": "<script>",
        }
    }
    book = p.parse_product_book(body(raw))
    assert len(book.bids) == 1 and book.malformed == 3 and book.time is None


@pytest.mark.parametrize(
    "raw", [{}, {"pricebook": None}, {"pricebook": {"bids": 1, "asks": []}}, []]
)
def test_book_shape_errors(raw: Any) -> None:
    with pytest.raises(p.ParseError):
        p.parse_product_book(body(raw))
