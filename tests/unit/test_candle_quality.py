"""Candle validation: every problem is detected and reported, no candle is invented or altered."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.adapters.coinbase_parse import Candle as Raw
from app.adapters.coinbase_parse import CandleSet
from app.market import candles as cd

G = 300
T0 = 1_780_000_200


def raw(
    i: int,
    *,
    o: str = "100",
    h: str = "101",
    low: str = "99",
    c: str = "100",
    v: str = "5",
    start: int | None = None,
) -> Raw:
    return Raw(
        T0 + i * G if start is None else start,
        Decimal(low),
        Decimal(h),
        Decimal(o),
        Decimal(c),
        Decimal(v),
    )


def run(
    items: list[Raw],
    *,
    n: int = 10,
    server: int | None = None,
    malformed: int = 0,
    unordered: bool = False,
) -> cd.QualityReport:
    return cd.validate_series(
        CandleSet(tuple(items), malformed, unordered),
        granularity=G,
        window_start=T0,
        window_end=T0 + n * G,
        server_epoch=server if server is not None else T0 + n * G + 60,
    )


def codes(report: cd.QualityReport) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in report.events:
        out[e.code] = out.get(e.code, 0) + e.count
    return out


def test_a_clean_series_passes_untouched() -> None:
    report = run([raw(i) for i in range(10)])
    assert len(report.candles) == 10 and report.events == ()
    assert (
        report.stats.gap_ratio == 0 and report.stats.present == 10 and report.stats.expected == 10
    )
    assert [c.start for c in report.candles] == [T0 + i * G for i in range(10)]


def test_gaps_are_reported_including_leading_and_trailing_and_never_filled() -> None:
    report = run([raw(i) for i in (2, 3, 6)])
    gaps = [(e.start, e.end, e.count) for e in report.events if e.code == cd.GAP]
    assert gaps == [(T0, T0 + 2 * G, 2), (T0 + 4 * G, T0 + 6 * G, 2), (T0 + 7 * G, T0 + 10 * G, 3)]
    assert [c.start for c in report.candles] == [
        T0 + 2 * G,
        T0 + 3 * G,
        T0 + 6 * G,
    ]  # nothing invented
    assert report.stats.missing == 7 and report.stats.gap_count == 3 and report.stats.max_gap == 3
    assert report.stats.gap_ratio == Decimal("0.7")


def test_an_empty_response_is_one_big_gap() -> None:
    report = run([])
    assert report.candles == () and codes(report) == {cd.GAP: 10}


def test_identical_duplicates_are_counted_and_collapsed() -> None:
    report = run([raw(i) for i in range(5)] + [raw(2), raw(2)])
    assert codes(report).get(cd.DUPLICATE) == 2 and len(report.candles) == 5


def test_conflicting_duplicates_are_excluded_entirely_not_picked() -> None:
    report = run([raw(0), raw(1), raw(1, c="100.5", h="101.5"), raw(2)])
    assert codes(report).get(cd.CONFLICT) == 2
    assert T0 + G not in [c.start for c in report.candles]
    assert [c.start for c in report.candles] == [T0, T0 + 2 * G]


def test_mixed_order_is_flagged_but_data_is_kept_sorted() -> None:
    report = run([raw(i) for i in range(4)], unordered=True)
    assert codes(report).get(cd.ORDER) == 1 and len(report.candles) == 4


def test_malformed_entries_are_counted() -> None:
    report = run([raw(i) for i in range(5)], malformed=3)
    assert codes(report).get(cd.MALFORMED) == 3


def test_invalid_ohlc_candles_are_dropped() -> None:
    bad = [
        raw(0, h="98"),  # high below low
        raw(1, o="102"),  # open above high
        raw(2, c="98"),  # close below low
        raw(3, low="0"),  # zero price
        raw(4, low="100.5"),  # low above open
    ]
    report = run(bad + [raw(9)])
    assert codes(report).get(cd.INVALID_OHLC) == 5 and [c.start for c in report.candles] == [
        T0 + 9 * G
    ]


def test_misaligned_and_future_candles_are_dropped() -> None:
    report = run(
        [raw(0), raw(1, start=T0 + G + 7), raw(2, start=T0 + 100 * G)], server=T0 + 10 * G + 60
    )
    assert codes(report).get(cd.MISALIGNED) == 1 and codes(report).get(cd.FUTURE) == 1
    assert len(report.candles) == 1


def test_open_candles_are_excluded_by_the_closed_rule() -> None:
    server = T0 + 5 * G + 2  # interval 4 closes at T0+5G, but only 3 s after that is safe
    report = run([raw(i) for i in range(6)], server=server)
    assert [c.start for c in report.candles] == [T0 + i * G for i in range(4)]
    assert codes(report).get(cd.OPEN_CANDLE) == 2
    assert report.effective_end == T0 + 5 * G - G + G - 0 or report.effective_end <= T0 + 5 * G
    closed_at_boundary = run([raw(i) for i in range(6)], server=T0 + 5 * G + cd.CLOSE_LAG_SECONDS)
    assert len(closed_at_boundary.candles) == 5


def test_candles_outside_the_requested_window_are_ignored_with_an_info_event() -> None:
    report = run(
        [raw(i) for i in range(10)] + [raw(0, start=T0 - G), raw(0, start=T0 + 10 * G)],
        server=T0 + 20 * G,
    )
    assert codes(report).get(cd.OUT_OF_WINDOW) == 2 and len(report.candles) == 10
    assert cd.SEVERITY[cd.OUT_OF_WINDOW] == "INFO"


def test_validation_never_changes_a_value() -> None:
    items = [
        raw(i, o="100.123456", h="101.5", low="99.25", c="100.75", v="7.000001") for i in range(4)
    ]
    report = run(items)
    for original, kept in zip(items, report.candles, strict=True):
        assert (kept.open, kept.high, kept.low, kept.close, kept.volume) == (
            original.open, original.high, original.low, original.close, original.volume,
        )  # fmt: skip


def test_the_result_is_a_subset_of_the_input_starts() -> None:
    items = [raw(i) for i in (0, 1, 1, 4, 7)] + [raw(2, h="0")]
    report = run(items)
    assert {c.start for c in report.candles} <= {i.start for i in items}


def test_events_have_fixed_codes_and_severities_and_no_free_text_from_input() -> None:
    report = run([raw(0), raw(1, h="98")], malformed=1, unordered=True)
    for e in report.events:
        assert (
            e.code in cd.EVENT_CODES
            and e.severity in {"INFO", "WARN", "ERROR"}
            and len(e.detail) <= 200
        )
    assert set(cd.SEVERITY) == set(cd.EVENT_CODES)


def test_series_gaps_and_ohlc_helpers() -> None:
    a = cd.convert(raw(0))
    b = replace(a, start=a.start + 3 * G)
    assert cd.series_gaps((a, b), G) == [(a.start + G, b.start)]
    assert cd.ohlc_valid(a) and not cd.ohlc_valid(replace(a, low=Decimal(200)))
    assert cd.last_closed_end(T0 + 5 * G + 10, G) == T0 + 5 * G
