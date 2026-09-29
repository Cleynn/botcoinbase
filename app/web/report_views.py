"""Display models for the Reports pages and the read-only market summary on the overview."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.storage.market_repositories import ReportRow
from app.storage.repositories import Repos
from app.web.view_models import fmt

PAGE_SIZE = 25


@dataclass(frozen=True)
class ReportListRow:
    id: str
    kind: str
    label: str
    title: str
    created: str
    sha: str


@dataclass(frozen=True)
class ReportsView:
    rows: tuple[ReportListRow, ...]
    page: int
    pages: int
    total: int


@dataclass(frozen=True)
class ReportView:
    id: str
    kind: str
    label: str
    title: str
    created: str
    sha256: str
    markdown: str


def build_list(rows: list[ReportRow], total: int, page: int) -> ReportsView:
    return ReportsView(
        tuple(
            ReportListRow(
                str(r.id), r.kind, r.mode_label, r.title, fmt(r.created_at), r.sha256[:16]
            )
            for r in rows
        ),
        page,
        max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
        total,
    )


def build_detail(row: ReportRow) -> ReportView:
    return ReportView(
        str(row.id),
        row.kind,
        row.mode_label,
        row.title,
        fmt(row.created_at),
        row.sha256,
        row.body_md,
    )


def market_rows(repos: Repos, now: datetime) -> tuple[tuple[str, str], ...]:
    """Read-only facts for the overview. Each label says whether it is BACKTEST or PAPER data."""
    rows: list[tuple[str, str]] = []
    count, last = repos.results.backtest_stats()
    rows.append(("Backtests stored (BACKTEST only)", str(count)))
    rows.append(("Last backtest (BACKTEST only)", fmt(last) if last else "None yet"))
    session = repos.paper.session()
    rows.append(("Paper session (PAPER only)", f"{session.state}, phase {session.phase}"))
    last_ingest = repos.market.last_success_at()
    rows.append(("Last successful candle import", fmt(last_ingest) if last_ingest else "None yet"))
    rows.append(
        ("Data-quality events (all imports)", str(sum(repos.market.event_counts().values())))
    )
    return tuple(rows)
