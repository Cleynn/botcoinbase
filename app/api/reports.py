"""Reports pages and downloads. Read-only: nothing here writes, trades or changes any state."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app.api.dependencies import get_services, require_permission
from app.domain.models import AuthContext
from app.domain.permissions import Permission
from app.web import report_views

router = APIRouter()
_access = require_permission(Permission.VIEW_REPORTS)


@router.get("/reports")
def reports_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_access)],
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
) -> Response:
    services = get_services(request)
    with services.storage.tx() as repos:
        rows = repos.results.reports(report_views.PAGE_SIZE, (page - 1) * report_views.PAGE_SIZE)
        total = repos.results.report_count()
    view = report_views.build_list(rows, total, page)
    return services.renderer.html("reports.html", auth=ctx, active="reports", view=view)


def _load(request: Request, report_id: UUID):  # type: ignore[no-untyped-def]
    with get_services(request).storage.tx() as repos:
        row = repos.results.report(report_id)
    if row is None:
        raise HTTPException(status_code=404)
    return row


@router.get("/reports/{report_id}")
def report_page(
    request: Request, report_id: UUID, ctx: Annotated[AuthContext, Depends(_access)]
) -> Response:
    row = _load(request, report_id)
    return get_services(request).renderer.html(
        "report_detail.html", auth=ctx, active="reports", view=report_views.build_detail(row)
    )


@router.get("/reports/{report_id}/json")
def report_json(
    request: Request, report_id: UUID, ctx: Annotated[AuthContext, Depends(_access)]
) -> Response:
    row = _load(request, report_id)
    return _download(row.body_json, f"report-{row.id}.json")


@router.get("/reports/{report_id}/md")
def report_md(
    request: Request, report_id: UUID, ctx: Annotated[AuthContext, Depends(_access)]
) -> Response:
    row = _load(request, report_id)
    return _download(row.body_md, f"report-{row.id}.md")


def _download(text: str, name: str) -> Response:
    # text/plain + attachment: the browser saves it, never renders it as a page or script
    return Response(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
