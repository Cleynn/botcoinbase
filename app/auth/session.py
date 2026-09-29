"""Opaque server-side sessions and the authentication service that orchestrates them.

The browser holds only a random 256-bit token in an HttpOnly cookie. The database stores its
SHA-256 digest, so a database read never yields a usable session. Every state change happens in
one transaction together with its audit event.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import SecretStr

from app.auth import csrf
from app.auth.audit import AuditWriter, actor_for
from app.auth.password import PasswordPolicyError, PasswordService, validate_new_password
from app.auth.rate_limit import LoginRateLimiter
from app.config import AuthSettings
from app.domain.enums import (
    AuditEventType,
    AuditResult,
    Role,
    SessionEndReason,
)
from app.domain.models import (
    AuditRecord,
    AuthContext,
    ChainStatus,
    ClientIdentity,
    Clock,
    SessionRecord,
    SessionSummary,
    User,
    UserSummary,
)
from app.domain.permissions import Permission
from app.storage.database import Storage
from app.storage.repositories import Repos

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
PURGE_AFTER = timedelta(days=2)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def valid_token_format(token: str | None) -> bool:
    return token is not None and TOKEN_RE.fullmatch(token) is not None


def derive_key(secret: SecretStr | str, purpose: str) -> bytes:
    raw = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
    return hmac.new(raw.encode("utf-8"), f"tradingdots/{purpose}".encode(), hashlib.sha256).digest()


@dataclass(frozen=True)
class LoginResult:
    status: Literal["ok", "invalid", "throttled"]
    token: str | None = None
    retry_after: int = 0


@dataclass(frozen=True)
class PasswordChangeResult:
    status: Literal["ok", "bad_current", "policy", "mismatch", "same", "throttled"]
    token: str | None = None
    codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityOverview:
    sessions: tuple[SessionSummary, ...]
    users: tuple[UserSummary, ...] | None
    reauth_active: bool


RevokeOutcome = Literal["ok", "reauth_required", "phrase_mismatch", "not_found"]


class AuthService:
    def __init__(
        self,
        *,
        storage: Storage,
        settings: AuthSettings,
        clock: Clock,
        passwords: PasswordService,
        limiter: LoginRateLimiter,
        audit: AuditWriter,
        csrf_key: bytes,
    ) -> None:
        self._storage = storage
        self._s = settings
        self._clock = clock
        self._passwords = passwords
        self._limiter = limiter
        self._audit = audit
        self._csrf_key = csrf_key

    # ------------------------------------------------------------------ helpers
    def _idle_cutoff(self, now: datetime) -> datetime:
        return now - timedelta(seconds=self._s.idle_timeout_seconds)

    def _open_session(
        self,
        repos: Repos,
        user: User,
        now: datetime,
        client: ClientIdentity,
        request_id: str | None,
    ) -> tuple[str, SessionRecord]:
        """Create a session, evicting the oldest ones beyond the per-user cap."""
        active = repos.sessions.list_active(user.id, now, self._idle_cutoff(now))
        overflow = len(active) - (self._s.max_sessions_per_user - 1)
        for old in list(reversed(active))[: max(overflow, 0)]:
            if repos.sessions.revoke(old.id, SessionEndReason.SESSION_LIMIT, now):
                self._audit.record(
                    repos,
                    AuditEventType.SESSION_REVOKED,
                    AuditResult.SUCCESS,
                    actor=actor_for(user),
                    target_type="session",
                    target_id=old.id,
                    reason=SessionEndReason.SESSION_LIMIT.value,
                    client_tag=client.tag,
                    request_id=request_id,
                )
        token = new_token()
        record = SessionRecord(
            id=uuid4(),
            user_id=user.id,
            token_hash=hash_token(token),
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + timedelta(seconds=self._s.absolute_timeout_seconds),
            revoked_at=None,
            revoked_reason=None,
            reauth_at=None,
        )
        repos.sessions.create(record)
        return token, record

    def _revoke_audited(
        self,
        repos: Repos,
        record: SessionRecord,
        reason: SessionEndReason,
        now: datetime,
        *,
        actor: User | None,
        event: AuditEventType,
        client: ClientIdentity | None,
        request_id: str | None,
        result: AuditResult = AuditResult.SUCCESS,
    ) -> None:
        if repos.sessions.revoke(record.id, reason, now):
            self._audit.record(
                repos,
                event,
                result,
                actor=actor_for(actor),
                target_type="session",
                target_id=record.id,
                reason=reason.value,
                client_tag=client.tag if client else None,
                request_id=request_id,
            )

    def csrf_token(self, token_hash: str) -> str:
        return csrf.session_token(self._csrf_key, token_hash)

    # ------------------------------------------------------------------ login
    def login(
        self,
        username: str,
        password: str,
        client: ClientIdentity,
        request_id: str | None,
        previous_token: str | None = None,
    ) -> LoginResult:
        name = username.strip().lower()
        now = self._clock.now()
        with self._storage.tx() as repos:
            keys = self._limiter.keys("login", name, client)
            throttle = self._limiter.check(repos, keys, now)
            if throttle.blocked:
                self._audit.record(
                    repos,
                    AuditEventType.LOGIN_THROTTLED,
                    AuditResult.DENIED,
                    reason="RATE_LIMITED",
                    client_tag=client.tag,
                    request_id=request_id,
                    detail={"identity": keys.account[:12]},
                    throttle_seconds=60,
                )
                return LoginResult("throttled", retry_after=throttle.retry_after)

            user = repos.users.get_by_username(name) if USERNAME_RE.fullmatch(name) else None
            reason = (
                "UNKNOWN_USER" if user is None else ("USER_DISABLED" if user.disabled_at else None)
            )
            if user is None or user.disabled_at is not None:
                self._passwords.burn(password)
                ok = False
            else:
                ok = self._passwords.verify(user.password_hash, password)
                reason = None if ok else "BAD_PASSWORD"

            if not ok or user is None:
                self._limiter.record_failure(repos, keys, now)
                self._audit.record(
                    repos,
                    AuditEventType.LOGIN_FAILURE,
                    AuditResult.FAILURE,
                    actor=actor_for(user),
                    reason=reason,
                    client_tag=client.tag,
                    request_id=request_id,
                    detail={"identity": keys.account[:12]},
                )
                return LoginResult("invalid")

            if self._passwords.needs_rehash(user.password_hash):
                repos.users.update_password(
                    user.id, self._passwords.hash(password), user.password_changed_at
                )
            self._limiter.record_success(repos, keys, now)
            repos.attempts.purge_before(now - PURGE_AFTER)

            if valid_token_format(previous_token):
                prior = repos.sessions.get_by_token_hash(hash_token(previous_token or ""))
                if prior is not None:
                    owner = repos.users.get_by_id(prior.user_id)
                    self._revoke_audited(
                        repos,
                        prior,
                        SessionEndReason.ROTATED,
                        now,
                        actor=owner,
                        event=AuditEventType.SESSION_REVOKED,
                        client=client,
                        request_id=request_id,
                    )
            token, record = self._open_session(repos, user, now, client, request_id)
            self._audit.record(
                repos,
                AuditEventType.LOGIN_SUCCESS,
                AuditResult.SUCCESS,
                actor=actor_for(user),
                target_type="session",
                target_id=record.id,
                client_tag=client.tag,
                request_id=request_id,
            )
            return LoginResult("ok", token=token)

    # ------------------------------------------------------------------ session validation
    def validate(
        self, token: str | None, *, touch: bool, client: ClientIdentity, request_id: str | None
    ) -> AuthContext | None:
        if not valid_token_format(token):
            return None
        token_hash = hash_token(token or "")
        now = self._clock.now()
        with self._storage.tx() as repos:
            record = repos.sessions.get_by_token_hash(token_hash)
            if record is None:
                self._audit.record(
                    repos,
                    AuditEventType.SESSION_REJECTED,
                    AuditResult.DENIED,
                    reason="UNKNOWN_SESSION",
                    client_tag=client.tag,
                    request_id=request_id,
                    throttle_seconds=60,
                )
                return None
            if record.revoked_at is not None:
                return None
            user = repos.users.get_by_id(record.user_id)
            if user is None or user.disabled_at is not None:
                self._revoke_audited(
                    repos,
                    record,
                    SessionEndReason.USER_DISABLED,
                    now,
                    actor=user,
                    event=AuditEventType.SESSION_REVOKED,
                    client=client,
                    request_id=request_id,
                )
                return None
            if now >= record.absolute_expires_at:
                self._revoke_audited(
                    repos,
                    record,
                    SessionEndReason.ABSOLUTE_EXPIRY,
                    now,
                    actor=user,
                    event=AuditEventType.SESSION_EXPIRED,
                    client=client,
                    request_id=request_id,
                    result=AuditResult.DENIED,
                )
                return None
            if now - record.last_seen_at >= timedelta(seconds=self._s.idle_timeout_seconds):
                self._revoke_audited(
                    repos,
                    record,
                    SessionEndReason.IDLE_TIMEOUT,
                    now,
                    actor=user,
                    event=AuditEventType.SESSION_EXPIRED,
                    client=client,
                    request_id=request_id,
                    result=AuditResult.DENIED,
                )
                return None
            if touch and now - record.last_seen_at >= timedelta(
                seconds=self._s.touch_interval_seconds
            ):
                repos.sessions.touch(record.id, now)
                record = SessionRecord(**{**record.__dict__, "last_seen_at": now})
            return AuthContext(user=user, session=record, csrf_token=self.csrf_token(token_hash))

    # ------------------------------------------------------------------ logout / revocation
    def logout(self, ctx: AuthContext, client: ClientIdentity, request_id: str | None) -> None:
        now = self._clock.now()
        with self._storage.tx() as repos:
            if repos.sessions.revoke(ctx.session.id, SessionEndReason.LOGOUT, now):
                self._audit.record(
                    repos,
                    AuditEventType.LOGOUT,
                    AuditResult.SUCCESS,
                    actor=actor_for(ctx.user),
                    target_type="session",
                    target_id=ctx.session.id,
                    client_tag=client.tag,
                    request_id=request_id,
                )

    def revoke_own_others(
        self, ctx: AuthContext, client: ClientIdentity, request_id: str | None
    ) -> int:
        now = self._clock.now()
        with self._storage.tx() as repos:
            count = repos.sessions.revoke_all_for_user(
                ctx.user.id, SessionEndReason.SELF_REVOKED, now, keep=ctx.session.id
            )
            self._audit.record(
                repos,
                AuditEventType.SESSION_REVOKED,
                AuditResult.SUCCESS,
                actor=actor_for(ctx.user),
                target_type="user",
                target_id=ctx.user.id,
                reason=SessionEndReason.SELF_REVOKED.value,
                client_tag=client.tag,
                request_id=request_id,
                detail={"count": count},
            )
            return count

    def revoke_own_session(
        self, ctx: AuthContext, session_id: UUID, client: ClientIdentity, request_id: str | None
    ) -> bool:
        """Revoke one of the caller's own other sessions. Never touches another user's session."""
        now = self._clock.now()
        with self._storage.tx() as repos:
            own = {
                s.id for s in repos.sessions.list_active(ctx.user.id, now, self._idle_cutoff(now))
            }
            if session_id not in own or session_id == ctx.session.id:
                return False
            repos.sessions.revoke(session_id, SessionEndReason.SELF_REVOKED, now)
            self._audit.record(
                repos,
                AuditEventType.SESSION_REVOKED,
                AuditResult.SUCCESS,
                actor=actor_for(ctx.user),
                target_type="session",
                target_id=session_id,
                reason=SessionEndReason.SELF_REVOKED.value,
                client_tag=client.tag,
                request_id=request_id,
            )
            return True

    # ------------------------------------------------------------------ password & reauth
    def _verify_current(
        self,
        repos: Repos,
        ctx: AuthContext,
        password: str,
        client: ClientIdentity,
        now: datetime,
        request_id: str | None,
        throttled_event: AuditEventType,
        namespace: str,
    ) -> Literal["ok", "bad", "throttled"]:
        keys = self._limiter.keys(namespace, f"user:{ctx.user.id}", client)
        if self._limiter.check(repos, keys, now).blocked:
            self._audit.record(
                repos,
                throttled_event,
                AuditResult.DENIED,
                actor=actor_for(ctx.user),
                reason="RATE_LIMITED",
                client_tag=client.tag,
                request_id=request_id,
                throttle_seconds=60,
            )
            return "throttled"
        if self._passwords.verify(ctx.user.password_hash, password):
            self._limiter.record_success(repos, keys, now)
            return "ok"
        self._limiter.record_failure(repos, keys, now)
        return "bad"

    def change_password(
        self,
        ctx: AuthContext,
        current: str,
        new: str,
        confirm: str,
        client: ClientIdentity,
        request_id: str | None,
    ) -> PasswordChangeResult:
        now = self._clock.now()
        with self._storage.tx() as repos:
            checked = self._verify_current(
                repos,
                ctx,
                current,
                client,
                now,
                request_id,
                AuditEventType.PASSWORD_CHANGE_FAILED,
                "pwchange",
            )
            if checked == "throttled":
                return PasswordChangeResult("throttled")

            def fail(
                status: Literal["bad_current", "policy", "mismatch", "same"],
                reason: str,
                codes: tuple[str, ...] = (),
            ) -> PasswordChangeResult:
                self._audit.record(
                    repos,
                    AuditEventType.PASSWORD_CHANGE_FAILED,
                    AuditResult.FAILURE,
                    actor=actor_for(ctx.user),
                    target_type="user",
                    target_id=ctx.user.id,
                    reason=reason,
                    client_tag=client.tag,
                    request_id=request_id,
                    detail={"rules": ",".join(codes)} if codes else None,
                )
                return PasswordChangeResult(status, codes=codes)

            if checked == "bad":
                return fail("bad_current", "BAD_CURRENT_PASSWORD")
            if not hmac.compare_digest(new.encode("utf-8"), confirm.encode("utf-8")):
                return fail("mismatch", "CONFIRMATION_MISMATCH")
            if hmac.compare_digest(new.encode("utf-8"), current.encode("utf-8")):
                return fail("same", "UNCHANGED")
            try:
                validate_new_password(new, ctx.user.username, self._s)
            except PasswordPolicyError as exc:
                return fail("policy", "POLICY", exc.codes)

            repos.users.update_password(ctx.user.id, self._passwords.hash(new), now)
            ended = repos.sessions.revoke_all_for_user(
                ctx.user.id, SessionEndReason.PASSWORD_CHANGED, now
            )
            token, record = self._open_session(repos, ctx.user, now, client, request_id)
            self._audit.record(
                repos,
                AuditEventType.PASSWORD_CHANGED,
                AuditResult.SUCCESS,
                actor=actor_for(ctx.user),
                target_type="user",
                target_id=ctx.user.id,
                client_tag=client.tag,
                request_id=request_id,
                detail={"sessions_ended": ended},
            )
            return PasswordChangeResult("ok", token=token)

    def reauthenticate(
        self, ctx: AuthContext, password: str, client: ClientIdentity, request_id: str | None
    ) -> Literal["ok", "invalid", "throttled"]:
        """Fresh-reauthentication helper: stamps the session so one sensitive action may follow."""
        now = self._clock.now()
        with self._storage.tx() as repos:
            checked = self._verify_current(
                repos,
                ctx,
                password,
                client,
                now,
                request_id,
                AuditEventType.REAUTH_THROTTLED,
                "reauth",
            )
            if checked == "throttled":
                return "throttled"
            if checked == "bad":
                self._audit.record(
                    repos,
                    AuditEventType.REAUTH_FAILURE,
                    AuditResult.FAILURE,
                    actor=actor_for(ctx.user),
                    reason="BAD_PASSWORD",
                    client_tag=client.tag,
                    request_id=request_id,
                )
                return "invalid"
            repos.sessions.set_reauth(ctx.session.id, now)
            self._audit.record(
                repos,
                AuditEventType.REAUTH_SUCCESS,
                AuditResult.SUCCESS,
                actor=actor_for(ctx.user),
                client_tag=client.tag,
                request_id=request_id,
            )
            return "ok"

    def consume_reauth(self, repos: Repos, ctx: AuthContext) -> bool:
        """Single-use: True at most once per successful reauthentication, within the window."""
        earliest = self._clock.now() - timedelta(seconds=self._s.reauth_window_seconds)
        return repos.sessions.consume_reauth(ctx.session.id, earliest)

    def reauth_is_fresh(self, ctx: AuthContext) -> bool:
        """Read-only: is a reauthentication currently available for one sensitive action?"""
        now = self._clock.now()
        with self._storage.tx() as repos:
            fresh = repos.sessions.get_by_token_hash(ctx.session.token_hash)
        return bool(
            fresh
            and fresh.reauth_at
            and now - fresh.reauth_at < timedelta(seconds=self._s.reauth_window_seconds)
        )

    @staticmethod
    def revoke_phrase(target: User) -> str:
        return f"REVOKE SESSIONS FOR {target.username.upper()}"

    def admin_revoke_user_sessions(
        self,
        ctx: AuthContext,
        target_id: UUID,
        confirmation: str,
        client: ClientIdentity,
        request_id: str | None,
    ) -> RevokeOutcome:
        """ADMIN only: exact typed phrase plus a fresh, single-use reauthentication."""
        from app.auth.authorization import authorize

        if not authorize(ctx.user.role, Permission.REVOKE_USER_SESSIONS):
            raise PermissionError("caller lacks REVOKE_USER_SESSIONS")
        now = self._clock.now()
        with self._storage.tx() as repos:
            target = repos.users.get_by_id(target_id)
            if target is None:
                return "not_found"

            def deny(reason: str) -> None:
                self._audit.record(
                    repos,
                    AuditEventType.ADMIN_SESSIONS_REVOKE_DENIED,
                    AuditResult.DENIED,
                    actor=actor_for(ctx.user),
                    target_type="user",
                    target_id=target.id,
                    reason=reason,
                    client_tag=client.tag,
                    request_id=request_id,
                )

            expected = self.revoke_phrase(target)
            typed_ok = confirmation.isascii() and hmac.compare_digest(
                confirmation.encode("ascii"), expected.encode("ascii")
            )
            if not typed_ok:
                deny("PHRASE_MISMATCH")
                return "phrase_mismatch"
            if not self.consume_reauth(repos, ctx):
                deny("REAUTH_REQUIRED")
                return "reauth_required"
            count = repos.sessions.revoke_all_for_user(
                target.id, SessionEndReason.ADMIN_REVOKED, now
            )
            self._audit.record(
                repos,
                AuditEventType.ADMIN_SESSIONS_REVOKED,
                AuditResult.SUCCESS,
                actor=actor_for(ctx.user),
                target_type="user",
                target_id=target.id,
                reason=SessionEndReason.ADMIN_REVOKED.value,
                client_tag=client.tag,
                request_id=request_id,
                detail={"count": count},
            )
            return "ok"

    # ------------------------------------------------------------------ read models
    def overview(self, ctx: AuthContext) -> SecurityOverview:
        now = self._clock.now()
        idle = self._idle_cutoff(now)
        with self._storage.tx() as repos:
            own = repos.sessions.list_active(ctx.user.id, now, idle)
            fresh = repos.sessions.get_by_token_hash(ctx.session.token_hash)
            users: tuple[UserSummary, ...] | None = None
            if ctx.user.role == Role.ADMIN:
                counts = repos.sessions.active_counts(now, idle)
                users = tuple(
                    UserSummary(
                        u.id, u.username, u.role, u.disabled_at is not None, counts.get(u.id, 0)
                    )
                    for u in repos.users.list_all()
                )
        reauth_active = bool(
            fresh
            and fresh.reauth_at
            and now - fresh.reauth_at < timedelta(seconds=self._s.reauth_window_seconds)
        )
        summaries = tuple(
            SessionSummary(
                s.id, s.created_at, s.last_seen_at, s.absolute_expires_at, s.id == ctx.session.id
            )
            for s in own
        )
        return SecurityOverview(summaries, users, reauth_active)

    def audit_page(
        self, *, code: AuditEventType | None, before: int | None, limit: int
    ) -> tuple[list[AuditRecord], ChainStatus]:
        """One page of events (limit + 1 rows to detect a next page) and the chain status."""
        with self._storage.tx() as repos:
            return repos.audit.list(
                code=code, before_seq=before, limit=limit + 1
            ), repos.audit.verify_chain()
