"""Authorization primitives: access declarations, the default-deny guard, and typed exceptions.

Every route must declare either `public` or a required permission. A route that declares neither
is refused at request time (fail closed) and flagged by the route-inventory test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.domain.enums import Role
from app.domain.permissions import Permission, has_permission

ACCESS_ATTR = "_td_access"


class LoginRequired(Exception):
    """No valid session: send the browser to the login page."""


class Forbidden(Exception):
    """Authenticated but not permitted."""


class CsrfRejected(Exception):
    """State-changing request failed the CSRF or Origin check."""


class UndeclaredAccess(Exception):
    """A route reached the guard without an access declaration (programming error)."""


@dataclass(frozen=True)
class AccessDeclaration:
    public: bool
    permission: Permission | None = None


PUBLIC = AccessDeclaration(public=True)


def declare(func: Callable[..., Any], declaration: AccessDeclaration) -> Callable[..., Any]:
    setattr(func, ACCESS_ATTR, declaration)
    return func


def authorize(role: Role | str | None, permission: Permission) -> bool:
    return has_permission(role, permission)


def _walk(dependant: Dependant) -> list[AccessDeclaration]:
    found: list[AccessDeclaration] = []
    call = dependant.call
    declaration = getattr(call, ACCESS_ATTR, None)
    if isinstance(declaration, AccessDeclaration):
        found.append(declaration)
    for sub in dependant.dependencies:
        found.extend(_walk(sub))
    return found


def declared_access(route: APIRoute) -> AccessDeclaration | None:
    """The access declaration on a route, or None. A conflicting mix is treated as undeclared."""
    declarations = _walk(route.dependant)
    unique = set(declarations)
    if len(unique) != 1:
        return None
    return declarations[0]
