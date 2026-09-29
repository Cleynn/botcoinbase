"""CSRF protection: session-bound HMAC tokens, a pre-session login token, and Origin checks.

Nothing is stored server-side for CSRF: tokens are derived from the (hashed) session token with a
server key, so they cannot be forged or replayed on another session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def session_token(key: bytes, session_token_hash: str) -> str:
    return _b64(
        hmac.new(
            key, b"csrf-session|" + session_token_hash.encode("ascii"), hashlib.sha256
        ).digest()
    )


def new_login_nonce() -> str:
    return secrets.token_urlsafe(32)


def valid_login_nonce(nonce: str | None) -> bool:
    return nonce is not None and _NONCE_RE.fullmatch(nonce) is not None


def login_token(key: bytes, nonce: str) -> str:
    return _b64(hmac.new(key, b"csrf-login|" + nonce.encode("ascii"), hashlib.sha256).digest())


def tokens_match(expected: str, supplied: str | None) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(expected.encode("ascii"), supplied.encode("utf-8", "replace"))


def origin_allowed(headers: Mapping[str, str], host: str, *, require_https: bool) -> bool:
    """Same-origin check for state-changing requests. Fails closed when no signal is present."""
    origin = headers.get("origin")
    if origin is not None:
        allowed = {f"https://{host}"} if require_https else {f"https://{host}", f"http://{host}"}
        return origin in allowed
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site in {"same-origin", "none"}
    return False
