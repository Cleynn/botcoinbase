"""Coinbase page. Read-only: two GET routes that show what the host feed stored.

The web process never contacts Coinbase; it reads the feed tables. Nothing here writes, trades or
changes any state, and the polled fragment does not extend the session.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.api.dependencies import get_services, require_permission
from app.domain.models import AuthContext
from app.domain.permissions import Permission
from app.web import exchange_views

router = APIRouter()
_access = require_permission(Permission.VIEW_BOT)
# Polling must not keep a session alive: no idle-timeout extension (touch=False).
_partial_access = require_permission(Permission.VIEW_BOT, touch=False)


def _view(request: Request) -> exchange_views.ExchangeView:
    services = get_services(request)
    with services.storage.tx() as repos:
        return exchange_views.build(repos, services.clock.now())


@router.get("/coinbase")
def exchange_page(request: Request, ctx: Annotated[AuthContext, Depends(_access)]) -> Response:
    return get_services(request).renderer.html(
        "exchange.html", auth=ctx, active="exchange", view=_view(request)
    )


@router.get("/partials/coinbase")
def exchange_partial(
    request: Request, ctx: Annotated[AuthContext, Depends(_partial_access)]
) -> Response:
    return get_services(request).renderer.html("exchange_data.html", view=_view(request))
