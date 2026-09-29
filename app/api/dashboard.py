"""Authenticated dashboard shell. Read-only: no control on this page can change any state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.api.dependencies import get_services, require_permission
from app.domain.models import AuthContext
from app.domain.permissions import Permission
from app.web.view_models import build_dashboard

router = APIRouter()

_dashboard_access = require_permission(Permission.VIEW_DASHBOARD)
# Polling must not keep a session alive: no idle-timeout extension (touch=False).
_partial_access = require_permission(Permission.VIEW_DASHBOARD, touch=False)


@router.get("/")
def dashboard(
    request: Request, ctx: Annotated[AuthContext, Depends(_dashboard_access)]
) -> Response:
    services = get_services(request)
    view = build_dashboard(services.settings, ctx, services.clock.now())
    return services.renderer.html(
        "dashboard.html", auth=ctx, active="overview", view=view, now=view.now
    )


@router.get("/partials/status")
def status_partial(
    request: Request, ctx: Annotated[AuthContext, Depends(_partial_access)]
) -> Response:
    services = get_services(request)
    return services.renderer.status_fragment(services.clock.now())
