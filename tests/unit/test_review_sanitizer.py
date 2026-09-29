"""Typed-field sanitizer, text scanner and manifest schema for review packages."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.review import sanitizer as sz
from app.review import schema

D = Decimal


# ------------------------------------------------------------------ typed fields
def test_decimals_are_canonical_finite_and_bounded() -> None:
    assert sz.decimal_str(D("1.50")) == "1.5"
    assert sz.decimal_str("0.0010") == "0.001"
    for bad in (float("1.5"), True, "abc", "NaN", D("Infinity"), D("1e40"), None, [1]):
        with pytest.raises(sz.Rejected):
            sz.decimal_str(bad)


def test_enums_ints_products_codes_and_hashes_are_exact() -> None:
    assert sz.enum("A", "B")("A") == "A"
    with pytest.raises(sz.Rejected):
        sz.enum("A")("a")
    assert sz.integer(0, 5)(5) == 5
    for bad in (6, -1, True, "1", 1.0):
        with pytest.raises(sz.Rejected):
            sz.integer(0, 5)(bad)
    assert sz.product("BTC-USDC") == "BTC-USDC"
    for bad in ("btc-usdc", "BTC-USD", "BTC-USDC\n", "<b>-USDC", "A" * 30 + "-USDC"):
        with pytest.raises(sz.Rejected):
            sz.product(bad)
    assert sz.sha256("a" * 64) == "a" * 64
    for bad in ("A" * 64, "a" * 63, "g" * 64):
        with pytest.raises(sz.Rejected):
            sz.sha256(bad)
    with pytest.raises(sz.Rejected):
        sz.code("lower")
    with pytest.raises(sz.Rejected):
        sz.uuid_str("not-a-uuid")


def test_timestamps_need_a_timezone_and_are_normalised_to_utc() -> None:
    assert sz.timestamp(datetime(2026, 9, 29, 12, 0, tzinfo=UTC)) == "2026-09-29T12:00:00Z"
    with pytest.raises(sz.Rejected):
        sz.timestamp(datetime(2026, 9, 29, 12, 0))  # noqa: DTZ001 - the naive value is the test
    with pytest.raises(sz.Rejected):
        sz.timestamp("2026-09-29")
    assert sz.epoch_ts(0) == "1970-01-01T00:00:00Z"
    assert sz.day(date(2026, 9, 29)) == "2026-09-29"


def test_a_row_is_rebuilt_from_its_schema_and_unknown_or_missing_keys_are_refused() -> None:
    schema_ = {"product": sz.product, "n": sz.integer(0, 9), "x": sz.optional(sz.decimal_str)}
    assert sz.sanitize_row(schema_, {"product": "BTC-USDC", "n": 1, "x": None}) == {
        "product": "BTC-USDC",
        "n": 1,
        "x": None,
    }
    with pytest.raises(sz.Rejected):
        sz.sanitize_row(schema_, {"product": "BTC-USDC", "n": 1, "x": None, "note": "free text"})
    with pytest.raises(sz.Rejected):
        sz.sanitize_row(schema_, {"product": "BTC-USDC", "n": 1})
    with pytest.raises(sz.Rejected):
        sz.sanitize_row(schema_, {"product": "BTC-USDC", "n": 1, "x": "free text"})


def test_no_exported_schema_has_a_free_text_field() -> None:
    from app.review.exporter import SCHEMAS

    assert SCHEMAS
    for path, fields in SCHEMAS.items():
        for name, check in fields.items():
            assert name not in {"title", "detail", "note", "message", "reason", "name", "url"}, path
            for probe in ("Traceback (most recent call last)", "password=hunter2", "<script>x"):
                try:
                    kept = check(probe)
                except sz.Rejected:
                    continue
                assert kept is None, (path, name)  # dropped, never copied through


# ------------------------------------------------------------------ scanner
@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("-----BEGIN " + "PRIVATE KEY-----", "PRIVATE_KEY"),
        ("see https://example.com/x", "URL"),
        ("postgresql://u:p@db:5432/x", "URL"),
        ("mail me at a.b@example.org", "EMAIL"),
        ("client 203.0.113.9 connected", "IPV4"),
        ("addr 2001:db8:0:0:1::1 up", "IPV6"),
        ("file /home/user/x.py", "FILE_PATH"),
        ('"path": "/data/snapshots/a.parquet"', "FILE_PATH"),
        ("C:\\Users\\x", "FILE_PATH"),
        ("Traceback (most recent call last):", "TRACEBACK"),
        ("eyJhbGciOiJIUzI1NiJ9abc", "JWT"),
        ("A" * 45, "LONG_TOKEN"),
        ("run: curl http", "ACTION_CAPABLE"),
        ("<script>alert(1)</script>", "ACTION_CAPABLE"),
        ("POST /api/v3/brokerage/orders", "ACTION_CAPABLE"),
        ("rm -rf /", "ACTION_CAPABLE"),
    ],
)
def test_the_scanner_finds_forbidden_content(text: str, rule: str) -> None:
    assert rule in sz.scan_text(text, strict=False)


@pytest.mark.parametrize(
    "text",
    [
        "password: hunter2",
        "Authorization: Bearer abc",
        "Cookie: session=1",
        "the api_key is x",
        "csrf value",
        "User-Agent: Mozilla",
        "a token here",
    ],
)
def test_strict_scanning_also_refuses_secret_words(text: str) -> None:
    assert "SECRET_WORD" in sz.scan_text(text, strict=True)
    assert "SECRET_WORD" not in sz.scan_text(text, strict=False)


def test_declared_digests_and_uuids_and_plain_numbers_are_not_false_positives() -> None:
    sha = "ab" * 32
    text = (
        f'{{"sha256":"{sha}","id":"123e4567-e89b-12d3-a456-426614174000",'
        '"price":"-12.5","v":"5.0.0","t":"2026-09-29T12:00:00Z","n":3}'
    )
    assert sz.scan_text(text) == []


def test_csv_cells_that_could_run_as_formulas_are_neutralised_but_numbers_are_kept() -> None:
    for cell in ("=1+1", "+cmd", "-cmd", "@SUM(A1)", "\t=x", "\r=x", "-1e5x"):
        assert sz.neutralise_cell(cell).startswith("'")
    for cell in ("-12.5", "3", "0.001", "BTC-USDC", ""):
        assert sz.neutralise_cell(cell) == cell


# ------------------------------------------------------------------ manifest schema
def good_manifest() -> dict[str, Any]:
    sha = "a" * 64
    return {
        "schema": schema.SCHEMA_ID,
        "package_id": "123e4567-e89b-12d3-a456-426614174000",
        "exporter_version": schema.EXPORTER_VERSION,
        "created_at": "2026-09-29T12:00:00Z",
        "period": {"start": "2026-09-01", "end": "2026-09-28"},
        "scope": ["backtests"],
        "advisory_only": True,
        "can_trade": False,
        "config_sha256": sha,
        "strategy_sha256": sha,
        "report_ids": ["123e4567-e89b-12d3-a456-426614174001"],
        "snapshots": [
            {
                "id": "123e4567-e89b-12d3-a456-426614174002",
                "file_sha256": sha,
                "content_sha256": sha,
            }
        ],
        "files": [{"path": "data/backtest_runs.jsonl", "bytes": 0, "sha256": sha, "rows": 0}],
        "not_available": ["risk_events"],
        "limitations": ["x"],
        "warning": schema.WARNING,
    }


def test_a_good_manifest_validates() -> None:
    schema.validate_manifest(good_manifest())


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda m: m.update(extra="x"), "MANIFEST_KEYS"),
        (lambda m: m.pop("warning"), "MANIFEST_KEYS"),
        (lambda m: m.update(can_trade=True), "MANIFEST_ADVISORY_FLAGS"),
        (lambda m: m.update(advisory_only=False), "MANIFEST_ADVISORY_FLAGS"),
        (lambda m: m.update(schema="other/1"), "MANIFEST_SCHEMA"),
        (lambda m: m.update(package_id="../../x"), "MANIFEST_IDENTITY"),
        (lambda m: m.update(config_sha256="x"), "MANIFEST_CONFIG_HASH"),
        (lambda m: m["period"].update(start="yesterday"), "MANIFEST_PERIOD"),
        (lambda m: m.update(scope=["everything"]), "MANIFEST_SCOPE"),
        (lambda m: m.update(scope=[]), "MANIFEST_SCOPE"),
        (lambda m: m.update(report_ids=["x"]), "MANIFEST_REPORT_IDS"),
        (lambda m: m["files"][0].update(path="../etc/passwd"), "MANIFEST_PATH"),
        (lambda m: m["files"][0].update(path="manifest.json"), "MANIFEST_PATH"),
        (lambda m: m["files"][0].update(path="data/Bad Name.jsonl"), "MANIFEST_PATH"),
        (lambda m: m["files"][0].update(extra=1), "MANIFEST_FILES"),
        (lambda m: m.update(files=[]), "MANIFEST_FILE_COUNT"),
        (lambda m: m["snapshots"][0].update(id="x"), "MANIFEST_SNAPSHOTS"),
    ],
)
def test_a_bad_manifest_is_refused_with_a_fixed_code(mutate: Any, code: str) -> None:
    m = copy.deepcopy(good_manifest())
    mutate(m)
    with pytest.raises(ValueError, match=code):
        schema.validate_manifest(m)


def test_only_listed_paths_are_allowed() -> None:
    ok = [
        "manifest.json",
        "README.md",
        "CHECKSUMS.sha256",
        "efficiency_summary.json",
        "prompt/llm_review_prompt.md",
        "data/backtest_runs.jsonl",
        "csv/summary.csv",
    ]
    bad = [
        "../x",
        "/etc/passwd",
        "data/../x.jsonl",
        "data/a/b.jsonl",
        "run.sh",
        "data/x.py",
        "data/UPPER.jsonl",
        "csv/x.csv/",
        "a.zip",
        ".env",
        "data/x.jsonl\n",
        "C:/x",
    ]
    assert all(schema.ALLOWED_PATH.match(p) for p in ok)
    assert not any(schema.ALLOWED_PATH.match(p) for p in bad)
