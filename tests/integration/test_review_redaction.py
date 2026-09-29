"""What a real package contains and, above all, what it never contains (SYNTHETIC data)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from app.auth.audit import AuditWriter
from app.backtest.service import BacktestService
from app.domain.enums import ActorRole, AuditEventType, AuditResult
from app.domain.models import AuditActor
from app.review import package as pkg
from app.review import sanitizer as sz
from app.review.builder import config_hashes
from app.review.schema import SCOPES
from tests.conftest import TestDb
from tests.integration.conftest import Market
from tests.integration.review_env import ReviewEnv
from tests.integration.test_review_service import enabled
from tests.integration.test_snapshots import freeze

Sql = Callable[..., list[dict[str, Any]]]
START = END = date(2026, 9, 29)  # the synthetic history is dated 'today' on the fake clock
HOSTILE = (
    "hunter2-PASSWORD",
    "postgresql://mallory:pw@203.0.113.7:5432/prod",
    "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
    "203.0.113.7",
    "/home/attacker/.ssh/id_rsa",
    "-----BEGIN " + "PRIVATE KEY-----",
    "<script>alert(1)</script>",
    "Traceback (most recent call last)",
    "mallory@example.org",
    "Mozilla/5.0 (X11; Linux) EvilBrowser",
)


def seed_real_content(review: ReviewEnv, mkt: Market, sql: Sql, storage: Any) -> None:
    """Backtests, reports, snapshots, ingest runs, plus hostile text in every free-text column."""
    mkt.imported()
    snap, _ = freeze(mkt)
    BacktestService(storage=mkt.storage, clock=mkt.clock, settings=mkt.settings).run(snap.id)
    hostile = " ".join(HOSTILE)
    body = json.dumps({"kind": "BACKTEST", "note": hostile})
    sql(
        "INSERT INTO reports (id, kind, mode_label, title, body_json, body_md, sha256, created_at) "
        "VALUES (%s, 'BACKTEST', 'BACKTEST', %s, %s, %s, %s, now())",
        (uuid4(), hostile[:120], body, hostile, hashlib.sha256(body.encode()).hexdigest()),
    )
    with storage.tx() as repos:  # hostile audit content in every free-text column
        AuditWriter(mkt.clock).record(
            repos,
            AuditEventType.AUTHZ_DENIED,
            AuditResult.DENIED,
            actor=AuditActor(review.admin.user.id, ActorRole.ADMIN),
            target_type="thing",
            target_id=HOSTILE[1],
            reason=HOSTILE[0],
            client_tag=HOSTILE[3][:32],
            request_id=HOSTILE[6][:64],
            detail={"note": HOSTILE[2], "path": HOSTILE[4], "agent": HOSTILE[9]},
        )


def files_of(blob: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def build_all(review: ReviewEnv, sql: Sql) -> tuple[Any, bytes]:
    enabled(review)
    review.reauth.available = True
    outcome = review.service.request(
        review.ctx, review.actor, "CREATE READ-ONLY REVIEW PACKAGE", START, END, list(SCOPES)
    )
    assert outcome.kind == "ok", outcome
    (result,) = review.builder.build_pending()
    assert result.state == "READY", result
    name = sql("SELECT storage_name FROM review_packages WHERE id = %s", (outcome.package_id,))[0][
        "storage_name"
    ]
    return outcome.package_id, (review.review_dir / name).read_bytes()


def test_no_hostile_text_secret_or_identity_reaches_a_package(
    review: ReviewEnv, mkt: Market, sql: Sql, storage: Any, db: TestDb, tmp_path: Any
) -> None:
    seed_real_content(review, mkt, sql, storage)
    _pid, blob = build_all(review, sql)
    files = files_of(blob)
    everything = b"\n".join(files.values()).decode("ascii")
    for needle in HOSTILE:
        assert needle not in everything, needle
    secrets_and_identities = (
        review.admin.username,
        str(review.admin.user.id),
        review.admin.user.password_hash,
        db.app_password,
        db.ctl_password,
        str(tmp_path),
        str(review.review_dir),
        "td_app",
        "td_ctl",
        "127.0.0.1",
        "session",
        "cookie",
        "csrf",
    )
    for needle in secrets_and_identities:
        assert needle.lower() not in everything.lower(), needle
    for name, data in files.items():
        strict = name not in ("README.md", "prompt/llm_review_prompt.md", "manifest.json")
        assert sz.scan_text(data.decode("ascii"), strict=strict) == [], name
    assert pkg.verify_zip(blob, max_bytes=20 * 1024 * 1024) == []


def test_the_package_holds_the_expected_history_and_provenance(
    review: ReviewEnv, mkt: Market, sql: Sql, storage: Any
) -> None:
    seed_real_content(review, mkt, sql, storage)
    pid, blob = build_all(review, sql)
    files = files_of(blob)
    manifest = json.loads(files["manifest.json"])
    assert manifest["package_id"] == str(pid) and manifest["can_trade"] is False
    runs = [json.loads(x) for x in files["data/backtest_runs.jsonl"].decode().splitlines()]
    assert sorted(r["fee_scenario"] for r in runs) == ["OPERATOR", "STRESS"]
    assert all(r["product"] == "BTC-USDC" and len(r["config_sha256"]) == 64 for r in runs)
    db_reports = {str(r["id"]) for r in sql("SELECT id FROM reports")}
    assert set(manifest["report_ids"]) == db_reports
    db_snaps = {
        str(r["id"]): r["file_sha256"] for r in sql("SELECT id, file_sha256 FROM dataset_snapshots")
    }
    assert {s["id"]: s["file_sha256"] for s in manifest["snapshots"]} == db_snaps
    config, strategy = config_hashes(review.settings)
    assert manifest["config_sha256"] == config and manifest["strategy_sha256"] == strategy
    quality = [json.loads(x) for x in files["data/ingest_runs.jsonl"].decode().splitlines()]
    assert quality and quality[0]["status"] == "COMPLETE"
    audit = [json.loads(x) for x in files["data/audit_summary.jsonl"].decode().splitlines()]
    assert audit and all(set(r) == {"day", "event_code", "result", "count"} for r in audit)
    assert "authz.denied" in {r["event_code"] for r in audit}
    summary = json.loads(files["efficiency_summary.json"])
    assert summary["backtests"]["BACKTEST/OPERATOR"]["runs"] == 1


def test_a_narrow_scope_contains_only_that_scope(
    review: ReviewEnv, mkt: Market, sql: Sql, storage: Any
) -> None:
    seed_real_content(review, mkt, sql, storage)
    enabled(review)
    review.reauth.available = True
    outcome = review.service.request(
        review.ctx, review.actor, "CREATE READ-ONLY REVIEW PACKAGE", START, END, ["data_quality"]
    )
    assert outcome.kind == "ok"
    review.builder.build_pending()
    name = sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    files = files_of((review.review_dir / name).read_bytes())
    assert {p for p in files if p.startswith("data/")} == {
        "data/ingest_runs.jsonl",
        "data/data_quality_events.jsonl",
    }
    assert b"BTC-USDC" in files["data/ingest_runs.jsonl"]


def test_pair_lifecycle_history_is_exported_as_codes_only(
    review: ReviewEnv, mkt: Market, sql: Sql, env: Any
) -> None:
    env.eligible("BTC-USDC")
    enabled(review)
    review.reauth.available = True
    review.service.request(
        review.ctx, review.actor, "CREATE READ-ONLY REVIEW PACKAGE", START, END, ["pairs"]
    )
    review.builder.build_pending()
    name = sql("SELECT storage_name FROM review_packages")[0]["storage_name"]
    files = files_of((review.review_dir / name).read_bytes())
    rows = [json.loads(x) for x in files["data/pair_lifecycle.jsonl"].decode().splitlines()]
    assert rows and {r["product"] for r in rows} == {"BTC-USDC"}
    assert any(r["state_after"] == "PAPER_ELIGIBLE" for r in rows)
    assert all(set(r) >= {"actor_class", "transition_no"} for r in rows)
    assert not any("actor_user_id" in r or "request_id" in r for r in rows)


def test_free_text_is_dropped_not_copied_when_it_could_appear_in_a_code_field() -> None:
    assert sz.code_or_none("PASS") == "PASS"
    for text in ("free text", "postgresql://x", None, "lower", 5):
        assert sz.code_or_none(text) is None


def test_csv_cells_of_a_real_package_are_neutralised(
    review: ReviewEnv, mkt: Market, sql: Sql, storage: Any
) -> None:
    seed_real_content(review, mkt, sql, storage)
    _pid, blob = build_all(review, sql)
    for name, data in files_of(blob).items():
        if name.endswith(".csv"):
            for line in data.decode().splitlines()[1:]:
                for cell in line.split(","):
                    assert sz.neutralise_cell(cell) == cell, (name, cell)


def test_scope_lists_with_unknown_or_repeated_items_are_refused(
    review: ReviewEnv,
) -> None:
    enabled(review)
    for scope in (["backtests", "bogus"], ["backtests", "backtests"], ["bogus"]):
        review.reauth.available = True
        got = review.service.request(
            review.ctx, review.actor, "CREATE READ-ONLY REVIEW PACKAGE", START, END, scope
        )
        assert got.kind == "invalid", scope


@pytest.mark.parametrize("flip", [1, 2, 3])
def test_corruption_is_never_silent_and_never_crashes_verification(
    review: ReviewEnv, mkt: Market, sql: Sql, storage: Any, flip: int
) -> None:
    """Any flipped byte is either reported, or changes no extracted content (ZIP metadata only);
    and with the stored package checksum, every flip is reported."""
    seed_real_content(review, mkt, sql, storage)
    _pid, blob = build_all(review, sql)
    original = files_of(blob)
    digest = pkg.sha256_hex(blob)
    for i in range(flip * 97, len(blob), max(len(blob) // 40, 1)):
        broken = bytearray(blob)
        broken[i] ^= 0xFF
        broken_bytes = bytes(broken)
        assert pkg.verify_zip(
            broken_bytes,
            max_bytes=20 * 1024 * 1024,
            expected={"package_sha256": digest},
        ), f"a flip at byte {i} passed the stored checksum"
        if not pkg.verify_zip(broken_bytes, max_bytes=20 * 1024 * 1024):
            assert files_of(broken_bytes) == original, f"flip at {i} silently changed content"
