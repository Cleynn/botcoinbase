"""Host-side package lifecycle: build, verify and retention cleanup (runs as td_ctl in `batch`).

Reads typed export views and writes only `review_packages` rows, one package file and audit events.
Nothing here touches bot, pair, strategy, risk, configuration, order, ledger or gate state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import Clock
from app.pairs.runner import HOST_ACTOR
from app.review import exporter, package
from app.review import sanitizer as sz
from app.review.schema import EXPORTER_VERSION
from app.review.store import PackageStore, StoreError
from app.storage.database import Storage
from app.storage.repositories import Repos
from app.storage.review_repositories import PackageRow

Made = tuple[bytes, dict[str, Any], bytes, dict[str, list[dict[str, Any]]]]
Evt = AuditEventType
Res = AuditResult


class ReviewBuildError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BuildResult:
    package_id: UUID
    state: str
    code: str | None = None


@dataclass(frozen=True)
class CleanupResult:
    expired: int
    removed: int
    orphans: int


def config_hashes(settings: Settings) -> tuple[str, str]:
    """SHA-256 of the full pair policy and of its strategy/simulation blocks (hashes only)."""
    policy = settings.pair_policy.model_dump(mode="json")
    strategy = {"strategy": policy.get("strategy"), "backtest": policy.get("backtest")}

    def digest(obj: Any) -> str:
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode("ascii")).hexdigest()

    return digest(policy), digest(strategy)


class ReviewBuilder:
    def __init__(self, *, storage: Storage, clock: Clock, settings: Settings) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._store = PackageStore(settings.review.dir)
        self._audit = AuditWriter(clock)

    # ------------------------------------------------------------------ audit helper
    def _record(
        self,
        repos: Repos,
        event: AuditEventType,
        result: AuditResult,
        package_id: UUID | str | None,
        reason: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._audit.record(
            repos,
            event,
            result,
            actor=HOST_CLI_ACTOR,
            target_type="review_package",
            target_id=package_id,
            reason=reason,
            client_tag=HOST_ACTOR.client_tag,
            request_id=HOST_ACTOR.request_id,
            detail=detail,
        )

    # ------------------------------------------------------------------ build
    def build_pending(self) -> list[BuildResult]:
        with self._storage.tx() as repos:
            pending = [p.id for p in repos.review.in_state("REQUESTED")]
        return [self._build_one(pid) for pid in pending]

    def _build_one(self, package_id: UUID) -> BuildResult:
        now = self._clock.now()
        with self._storage.tx() as repos:
            row = repos.review.package(package_id, for_update=True)
            if row is None or row.state != "REQUESTED":
                return BuildResult(package_id, row.state if row else "MISSING")
            if not repos.review.settings().enabled:
                return self._fail(repos, row, "FEATURE_DISABLED")
            repos.review.mark_generating(package_id, now)
            self._record(
                repos, AuditEventType.REVIEW_GENERATING, AuditResult.SUCCESS, package_id, None
            )
        storage_name: str | None = None
        try:
            blob, manifest, manifest_bytes, data = self._make(row)
            problems = package.verify_zip(
                blob,
                max_bytes=self._settings.review.max_bytes,
                max_rows=self._settings.review.max_rows_per_file,
            )
            if problems:
                raise ReviewBuildError(
                    "SCANNER_HIT" if "REDACTION_FAILED" in problems else "SELF_CHECK_FAILED"
                )
            storage_name = f"{uuid4()}.zip"
            self._store.write(storage_name, blob)
        except sz.Rejected as exc:
            return self._finish_failed(
                package_id, "SCANNER_HIT" if "SCAN" in exc.code else exc.code
            )
        except ReviewBuildError as exc:
            return self._finish_failed(package_id, exc.code)
        except Exception:
            return self._finish_failed(package_id, "EXPORT_ERROR")
        done = self._clock.now()
        try:
            with self._storage.tx() as repos:
                repos.review.mark_ready(
                    package_id,
                    storage_name=storage_name,
                    size_bytes=len(blob),
                    package_sha256=package.sha256_hex(blob),
                    manifest_sha256=package.sha256_hex(manifest_bytes),
                    config_sha256=manifest["config_sha256"],
                    exporter_version=EXPORTER_VERSION,
                    files=manifest["files"],
                    now=done,
                    expires_at=done + timedelta(days=row.retention_days),
                )
                self._record(
                    repos,
                    AuditEventType.REVIEW_READY,
                    AuditResult.SUCCESS,
                    package_id,
                    None,
                    {
                        "files": len(manifest["files"]),
                        "bytes": len(blob),
                        "rows": sum(map(len, data.values())),
                    },
                )
        except Exception:
            self._store.remove(storage_name)
            return self._finish_failed(package_id, "RECORD_ERROR")
        return BuildResult(package_id, "READY")

    def _make(
        self, row: PackageRow
    ) -> tuple[bytes, dict[str, Any], bytes, dict[str, list[dict[str, Any]]]]:
        review = self._settings.review
        if (row.period_end - row.period_start).days > review.max_period_days:
            raise ReviewBuildError("PERIOD_TOO_LONG")
        with self._storage.tx() as repos:
            data = exporter.export(
                repos, row.scope, row.period_start, row.period_end, review.max_rows_per_file
            )
        config, strategy = config_hashes(self._settings)
        blob, manifest, manifest_bytes = package.build_zip(
            package_id=str(row.id),
            created_at=self._clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            start=row.period_start,
            end=row.period_end,
            scope=tuple(row.scope),
            data=data,
            config_sha256=config,
            strategy_sha256=strategy,
        )
        if len(blob) > review.max_bytes:
            raise ReviewBuildError("TOO_LARGE")
        return blob, manifest, manifest_bytes, data

    def _fail(self, repos: Repos, row: PackageRow, code: str) -> BuildResult:
        repos.review.mark_failed(row.id, code, self._clock.now())
        self._record(repos, AuditEventType.REVIEW_FAILED, AuditResult.FAILURE, row.id, code)
        return BuildResult(row.id, "FAILED", code)

    def _finish_failed(self, package_id: UUID, code: str) -> BuildResult:
        with self._storage.tx() as repos:
            row = repos.review.package(package_id, for_update=True)
            if row is None:
                return BuildResult(package_id, "MISSING", code)
            return self._fail(repos, row, code)

    # ------------------------------------------------------------------ verify
    def verify(self, package_id: UUID) -> list[str]:
        """Re-verify one READY package on disk. A failure marks it CORRUPT (and audits it)."""
        with self._storage.tx() as repos:
            row = repos.review.package(package_id)
        if row is None or row.state != "READY" or row.storage_name is None:
            return ["NOT_READY"]
        problems = verify_row(row, self._store, self._settings)
        with self._storage.tx() as repos:
            if problems:
                repos.review.mark_corrupt(package_id, self._clock.now())
                self._record(
                    repos,
                    AuditEventType.REVIEW_CORRUPT,
                    AuditResult.FAILURE,
                    package_id,
                    problems[0],
                    {"problems": len(problems)},
                )
            else:
                self._record(
                    repos, AuditEventType.REVIEW_VERIFIED, AuditResult.SUCCESS, package_id, None
                )
        return problems

    def verify_all(self) -> dict[UUID, list[str]]:
        with self._storage.tx() as repos:
            ids = [p.id for p in repos.review.in_state("READY")]
        return {pid: self.verify(pid) for pid in ids}

    # ------------------------------------------------------------------ retention cleanup
    def cleanup(self) -> CleanupResult:
        """Expire packages past retention (state first), then remove their content, then orphans."""
        now = self._clock.now()
        expired = removed = 0
        with self._storage.tx() as repos:
            for row in repos.review.due_for_expiry(now):
                repos.review.mark_expired(row.id)
                self._record(
                    repos, AuditEventType.REVIEW_EXPIRED, AuditResult.SUCCESS, row.id, None
                )
                expired += 1
        with self._storage.tx() as repos:
            pending = repos.review.expired_with_content()
        for row in pending:
            existed = self._store.remove(row.storage_name or "")
            with self._storage.tx() as repos:
                repos.review.mark_content_removed(row.id, self._clock.now())
                self._record(
                    repos,
                    AuditEventType.REVIEW_CLEANUP,
                    AuditResult.SUCCESS,
                    row.id,
                    "CONTENT_REMOVED",
                    {"file_existed": existed},
                )
            removed += 1
        with self._storage.tx() as repos:
            keep = repos.review.storage_names()
        orphans = 0
        for name in self._store.stray_files(keep):
            self._store.remove_stray(name)
            orphans += 1
        if orphans:
            with self._storage.tx() as repos:
                self._record(
                    repos,
                    AuditEventType.REVIEW_CLEANUP,
                    AuditResult.SUCCESS,
                    None,
                    "ORPHANS_REMOVED",
                    {"files": orphans},
                )
        return CleanupResult(expired, removed, orphans)


def read_and_verify(
    row: PackageRow, store: PackageStore, settings: Settings
) -> tuple[list[str], bytes]:
    """Read a READY package once and verify exactly those bytes against its database row.

    Returns (problems, bytes); the bytes are only meant to be served when there are no problems.
    """
    if row.storage_name is None:
        return ["NOT_READY"], b""
    try:
        blob = store.read(row.storage_name, settings.review.max_bytes)
    except StoreError as exc:
        return [f"FILE_{exc.code}"], b""
    problems = package.verify_zip(
        blob,
        max_bytes=settings.review.max_bytes,
        max_rows=settings.review.max_rows_per_file,
        expected={
            "id": row.id,
            "package_sha256": row.package_sha256,
            "manifest_sha256": row.manifest_sha256,
            "config_sha256": row.config_sha256,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "scope": list(row.scope),
        },
    )
    return problems, blob


def verify_row(row: PackageRow, store: PackageStore, settings: Settings) -> list[str]:
    return read_and_verify(row, store, settings)[0]
