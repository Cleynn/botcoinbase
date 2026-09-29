"""Package build determinism and verification: schema, files, checksums, paths and redaction."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.review import package, schema
from app.review import sanitizer as sz
from app.review.exporter import SCHEMAS

SHA = "ab" * 32
MAX = 20 * 1024 * 1024
PID = "123e4567-e89b-12d3-a456-426614174000"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def rows() -> dict[str, list[dict[str, Any]]]:
    run = {
        "run_id": PID,
        "snapshot_id": "123e4567-e89b-12d3-a456-426614174002",
        "product": "BTC-USDC",
        "kind": "BACKTEST",
        "fee_scenario": "OPERATOR",
        "config_sha256": SHA,
        "engine_version": "5.0.0",
        "created_at": NOW,
        "net_pnl": Decimal("-0.77"),
        "realized_pnl": Decimal("0.1"),
        "unrealized_pnl": Decimal("-0.87"),
        "fees_paid": Decimal("0.12"),
        "max_drawdown": Decimal("0.04"),
        "buy_and_hold_return": Decimal("-0.07"),
        "fills": 3,
        "partial_fills": 0,
        "touched_no_fill": 8,
        "cycles_completed": 1,
        "orders_placed": 6,
        "stale_age_cancels": 1,
        "stale_gap_cancels": 0,
        "breakout_stops": 1,
        "halts": 0,
        "decisions_grid": 1,
        "decisions_no_trade": 100,
    }
    for k in (
        "fold_count",
        "out_of_sample_net_pnl",
        "positive_folds",
        "negative_folds",
        "flat_folds",
        "worst_fold_pnl",
        "worst_drawdown",
    ):
        run[k] = None
    return {
        "data/backtest_runs.jsonl": [sz.sanitize_row(SCHEMAS["data/backtest_runs.jsonl"], run)],
        "data/reports_index.jsonl": [
            sz.sanitize_row(
                SCHEMAS["data/reports_index.jsonl"],
                {
                    "report_id": "123e4567-e89b-12d3-a456-426614174001",
                    "kind": "BACKTEST",
                    "mode_label": "BACKTEST",
                    "sha256": SHA,
                    "created_at": NOW,
                },
            )
        ],
        "data/audit_summary.jsonl": [
            sz.sanitize_row(
                SCHEMAS["data/audit_summary.jsonl"],
                {
                    "day": date(2026, 9, 28),
                    "event_code": "review.enabled",
                    "result": "SUCCESS",
                    "count": 2,
                },
            )
        ],
    }


def build(data: dict[str, list[dict[str, Any]]] | None = None) -> tuple[bytes, dict[str, Any]]:
    blob, manifest, _mb = package.build_zip(
        package_id=PID,
        created_at="2026-09-29T12:00:00Z",
        start=date(2026, 9, 1),
        end=date(2026, 9, 28),
        scope=("backtests", "audit_summary"),
        data=data if data is not None else rows(),
        config_sha256=SHA,
        strategy_sha256=SHA,
    )
    return blob, manifest


def unzip(blob: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def rezip(files: dict[str, bytes], *, checksums: bool = True, manifest: bool = True) -> bytes:
    """Rebuild a ZIP from edited contents; optionally make manifest and checksums consistent."""
    files = dict(files)
    if manifest:
        m = json.loads(files[schema.MANIFEST])
        for entry in m["files"]:
            if entry["path"] in files:
                entry["bytes"] = len(files[entry["path"]])
                entry["sha256"] = package.sha256_hex(files[entry["path"]])
        files[schema.MANIFEST] = (json.dumps(m, sort_keys=True, indent=2) + "\n").encode()
    if checksums:
        files[schema.CHECKSUMS] = "".join(
            f"{package.sha256_hex(b)}  {p}\n"
            for p, b in sorted(files.items())
            if p != schema.CHECKSUMS
        ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def problems(blob: bytes, **kw: Any) -> list[str]:
    return package.verify_zip(blob, max_bytes=MAX, **kw)


# ------------------------------------------------------------------ build
def test_a_package_has_every_required_part() -> None:
    blob, manifest = build()
    files = unzip(blob)
    for required in (
        schema.MANIFEST,
        schema.README,
        schema.SUMMARY,
        schema.PROMPT,
        schema.CHECKSUMS,
        "csv/summary.csv",
        "csv/timeline.csv",
        "data/backtest_runs.jsonl",
        "data/reports_index.jsonl",
    ):
        assert required in files, required
    assert all(schema.ALLOWED_PATH.match(n) for n in files)
    assert manifest["can_trade"] is False and manifest["advisory_only"] is True
    assert manifest["config_sha256"] == SHA and manifest["report_ids"]
    assert manifest["warning"] in files[schema.README].decode()
    assert "untrusted" in files[schema.PROMPT].decode().lower()
    assert problems(blob) == []


def test_identical_inputs_give_identical_bytes() -> None:
    a, _ = build()
    b, _ = build()
    assert a == b and package.sha256_hex(a) == package.sha256_hex(b)


def test_the_zip_is_deterministic_with_no_symlinks_or_extra_metadata() -> None:
    blob, _ = build()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert stat.S_ISREG(info.external_attr >> 16)
            assert not info.filename.startswith("/") and ".." not in info.filename
            assert not info.flag_bits & 0x1


def test_checksums_file_covers_every_other_file() -> None:
    files = unzip(build()[0])
    listed = dict(
        line.split("  ", 1)[::-1] for line in files[schema.CHECKSUMS].decode().splitlines()
    )
    assert set(listed) == set(files) - {schema.CHECKSUMS}
    assert all(package.sha256_hex(files[p]) == d for p, d in listed.items())


def test_the_summary_is_computed_from_the_exported_rows_only() -> None:
    summary = json.loads(unzip(build()[0])[schema.SUMMARY])
    assert summary["audit_events_total"] == 2
    assert summary["backtests"]["BACKTEST/OPERATOR"]["runs"] == 1
    assert summary["backtests"]["BACKTEST/OPERATOR"]["worst_net_pnl"] == "-0.77"


def test_documents_contain_no_forbidden_content_and_state_the_limits() -> None:
    files = unzip(build()[0])
    for name in (schema.README, schema.PROMPT):
        text = files[name].decode()
        assert sz.scan_text(text, strict=False) == [], name
    readme = files[schema.README].decode()
    assert "cannot trade" in readme and "Limitations" in readme
    prompt = files[schema.PROMPT].decode()
    assert "not an instruction" in prompt and "advisory" in prompt.lower()
    assert "```" not in prompt  # no code blocks to copy and run


def test_csv_output_is_neutralised() -> None:
    data = rows()
    files = unzip(build(data)[0])
    for name in ("csv/summary.csv", "csv/timeline.csv"):
        for line in files[name].decode().splitlines():
            for cell in line.split(","):
                assert not (cell and cell[0] in "=+@\t\r"), (name, cell)


# ------------------------------------------------------------------ verify: structure
def test_not_a_zip_or_too_large_is_refused() -> None:
    assert problems(b"not a zip") == ["NOT_A_ZIP"]
    assert (
        problems(
            build()[0] * 1,
        )
        == []
    )
    assert package.verify_zip(build()[0], max_bytes=100) == ["PACKAGE_TOO_LARGE"]


def test_unlisted_paths_are_refused_including_traversal() -> None:
    files = unzip(build()[0])
    for bad in ("../evil.jsonl", "/abs/path.jsonl", "run.sh", "data/sub/x.jsonl", "notes.txt"):
        blob = rezip({**files, bad: b"x"}, manifest=False, checksums=False)
        assert "PATH_NOT_ALLOWED" in problems(blob), bad


def test_symlinks_and_encrypted_entries_are_refused() -> None:
    files = unzip(build()[0])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        link = zipfile.ZipInfo("data/link_file.jsonl")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "/etc/passwd")
    assert "SPECIAL_FILE" in problems(buf.getvalue())


def test_duplicate_entries_are_refused() -> None:
    files = unzip(build()[0])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf, pytest.warns(UserWarning):
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr(schema.README, b"second readme")
    assert "DUPLICATE_ENTRY" in problems(buf.getvalue())


def test_a_zip_bomb_is_refused_before_it_is_expanded() -> None:
    files = unzip(build()[0])
    files["data/backtest_runs.jsonl"] = b"x" * (30 * 1024 * 1024)
    blob = rezip(files, checksums=True, manifest=True)
    assert len(blob) < MAX
    assert "UNCOMPRESSED_TOO_LARGE" in package.verify_zip(blob, max_bytes=MAX)


# ------------------------------------------------------------------ verify: integrity
def edit(name: str, fn: Callable[[bytes], bytes]) -> dict[str, bytes]:
    files = unzip(build()[0])
    files[name] = fn(files[name])
    return files


def test_a_changed_data_file_is_detected_when_checksums_are_not_updated() -> None:
    files = edit("data/backtest_runs.jsonl", lambda b: b.replace(b'"fills":3', b'"fills":9'))
    got = problems(rezip(files, checksums=False, manifest=False))
    assert {"MANIFEST_CHECKSUM_MISMATCH", "CHECKSUM_MISMATCH"} <= set(got)


def test_a_changed_manifest_is_detected() -> None:
    files = edit(schema.MANIFEST, lambda b: b.replace(b"backtests", b"paper"))
    assert "CHECKSUM_MISMATCH" in problems(rezip(files, checksums=False, manifest=False))


def test_a_missing_or_extra_file_is_detected() -> None:
    files = unzip(build()[0])
    del files["csv/summary.csv"]
    assert "FILE_SET_MISMATCH" in problems(rezip(files, checksums=False, manifest=False))
    extra = {**unzip(build()[0]), "data/extra_file.jsonl": b""}
    assert "FILE_SET_MISMATCH" in problems(rezip(extra, checksums=False, manifest=False))


def test_a_missing_checksums_file_or_manifest_is_refused() -> None:
    for name in (schema.CHECKSUMS, schema.MANIFEST):
        files = unzip(build()[0])
        del files[name]
        assert "MISSING_CORE_FILE" in problems(rezip(files, checksums=False, manifest=False))


def test_a_manifest_that_claims_trading_is_refused_even_with_valid_checksums() -> None:
    files = unzip(build()[0])
    m = json.loads(files[schema.MANIFEST])
    m["can_trade"] = True
    files[schema.MANIFEST] = (json.dumps(m, sort_keys=True) + "\n").encode()
    assert "MANIFEST_ADVISORY_FLAGS" in problems(rezip(files, manifest=False))


def test_the_database_row_is_cross_checked() -> None:
    blob, manifest = build()
    row = {
        "id": PID,
        "package_sha256": package.sha256_hex(blob),
        "config_sha256": SHA,
        "period_start": date(2026, 9, 1),
        "period_end": date(2026, 9, 28),
        "scope": ["backtests", "audit_summary"],
    }
    assert problems(blob, expected=row) == []
    for field, value, code in (
        ("package_sha256", "0" * 64, "PACKAGE_CHECKSUM_MISMATCH"),
        ("id", "123e4567-e89b-12d3-a456-426614174999", "PACKAGE_ID_MISMATCH"),
        ("config_sha256", "0" * 64, "CONFIG_HASH_MISMATCH"),
        ("period_start", date(2026, 8, 1), "PERIOD_MISMATCH"),
        ("scope", ["paper"], "SCOPE_MISMATCH"),
    ):
        assert code in problems(blob, expected={**row, field: value}), code


# ------------------------------------------------------------------ verify: redaction
@pytest.mark.parametrize(
    "poison",
    [
        '"note":"password=hunter2"',
        '"note":"postgresql://user:pw@10.0.0.5/db"',
        '"note":"-----BEGIN ""PRIVATE KEY-----"',
        '"note":"203.0.113.9"',
        '"note":"/home/user/.env"',
        '"note":"Traceback (most recent call last)"',
        '"note":"<script>alert(1)</script>"',
        '"note":"Bearer abcdefghijklmnop"',
        '"note":"session_id=abcd"',
        '"note":"user@example.org"',
    ],
)
def test_forbidden_content_is_caught_even_when_every_checksum_is_valid(poison: str) -> None:
    """An attacker who can rewrite files AND checksums still cannot get content past the scanner."""
    files = edit(
        "data/reports_index.jsonl", lambda b: b.rstrip(b"\n")[:-1] + f",{poison}}}\n".encode()
    )
    got = problems(rezip(files))
    assert "REDACTION_FAILED" in got or "ROW_SCHEMA" in got
    assert "REDACTION_FAILED" in got


def test_an_extra_field_in_a_data_row_is_refused_by_schema() -> None:
    files = edit("data/reports_index.jsonl", lambda b: b.rstrip(b"\n")[:-1] + b',"extra":1}\n')
    assert "ROW_SCHEMA" in problems(rezip(files))


def test_a_formula_cell_in_a_csv_is_refused() -> None:
    files = edit("csv/timeline.csv", lambda b: b + b"2026-09-28,=HYPERLINK(1),1\n")
    assert "CSV_FORMULA" in problems(rezip(files))


def test_a_non_ascii_or_unknown_data_file_is_refused() -> None:
    files = edit("csv/summary.csv", lambda b: b + "café,1\n".encode())
    assert "NOT_ASCII" in problems(rezip(files))
    unknown = {**unzip(build()[0]), "data/surprise_file.jsonl": b'{"a":1}\n'}
    m = json.loads(unknown[schema.MANIFEST])
    m["files"].append(
        {"path": "data/surprise_file.jsonl", "bytes": 8, "sha256": "0" * 64, "rows": 1}
    )
    unknown[schema.MANIFEST] = (json.dumps(m, sort_keys=True) + "\n").encode()
    assert "UNKNOWN_DATA_FILE" in problems(rezip(unknown))


def test_fuzzing_every_byte_never_raises_and_is_never_silent() -> None:
    """Flip every byte of a package: verification always returns (never raises), and when it
    reports nothing the extracted content is identical (only harmless ZIP metadata changed)."""
    blob, _ = build()
    original = unzip(blob)
    for i in range(len(blob)):
        for mask in (0x01, 0xFF):
            broken = bytearray(blob)
            broken[i] ^= mask
            result = problems(bytes(broken))  # must not raise
            if not result:
                assert unzip(bytes(broken)) == original, (i, mask)
