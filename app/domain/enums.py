"""Enumerations shared across layers. String values are stored in the database (CHECK lists)."""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "ADMIN"
    VIEWER = "VIEWER"


class ActorRole(StrEnum):
    """Who performed an audited action. HOST_CLI is the local interactive host CLI."""

    ADMIN = "ADMIN"
    VIEWER = "VIEWER"
    HOST_CLI = "HOST_CLI"


class AuditResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"


class LoginScope(StrEnum):
    PAIR = "PAIR"  # one account from one client
    CLIENT = "CLIENT"  # one client across accounts
    ACCOUNT = "ACCOUNT"  # one account from any client


class SessionEndReason(StrEnum):
    LOGOUT = "LOGOUT"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"
    ABSOLUTE_EXPIRY = "ABSOLUTE_EXPIRY"
    ADMIN_REVOKED = "ADMIN_REVOKED"
    SELF_REVOKED = "SELF_REVOKED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    ROTATED = "ROTATED"
    SESSION_LIMIT = "SESSION_LIMIT"
    USER_DISABLED = "USER_DISABLED"


class AuditEventType(StrEnum):
    """The audit event catalogue. Adding a code here is the only way to emit a new event."""

    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGIN_THROTTLED = "auth.login.throttled"
    LOGOUT = "auth.logout"
    SESSION_EXPIRED = "session.expired"
    SESSION_REVOKED = "session.revoked"
    SESSION_REJECTED = "session.rejected"
    PASSWORD_CHANGED = "auth.password.changed"
    PASSWORD_CHANGE_FAILED = "auth.password.change_failed"
    REAUTH_SUCCESS = "auth.reauth.success"
    REAUTH_FAILURE = "auth.reauth.failure"
    REAUTH_THROTTLED = "auth.reauth.throttled"
    AUTHZ_DENIED = "authz.denied"
    CSRF_REJECTED = "csrf.rejected"
    ADMIN_SESSIONS_REVOKED = "admin.sessions_revoked"
    ADMIN_SESSIONS_REVOKE_DENIED = "admin.sessions_revoke_denied"
    ADMIN_CREATED = "admin.created"
    ADMIN_PASSWORD_ROTATED = "admin.password_rotated"
    PRODUCT_DISCOVERED = "product.discovered"
    PAIR_PROPOSED = "pair.proposed"
    PAIR_VALIDATION_STARTED = "pair.validation_started"
    PAIR_RESEARCH_ONLY = "pair.research_only"
    PAIR_PAPER_ELIGIBLE = "pair.paper_eligible"
    PAIR_ELIGIBILITY_EXPIRED = "pair.eligibility_expired"
    PAIR_ACTIVATED_PAPER = "pair.activated_paper"
    PAIR_PAUSED = "pair.paused"
    PAIR_RESUMED_PAPER = "pair.resumed_paper"
    PAIR_DEACTIVATED = "pair.deactivated"
    PAIR_DISABLED = "pair.disabled"
    PAIR_REENABLED = "pair.reenabled"
    PAIR_ARCHIVED = "pair.archived"
    PAIR_TRANSITION_DENIED = "pair.transition_denied"
    MARKET_INGESTED = "market.ingested"
    MARKET_SNAPSHOT_CREATED = "market.snapshot_created"
    BACKTEST_COMPLETED = "backtest.completed"
    REPORT_CREATED = "report.created"
    PAPER_STARTED = "paper.started"
    PAPER_STOPPED = "paper.stopped"
    PAPER_STEPPED = "paper.stepped"
    REVIEW_ENABLED = "review.enabled"
    REVIEW_DISABLED = "review.disabled"
    REVIEW_REQUESTED = "review.requested"
    REVIEW_GENERATING = "review.generating"
    REVIEW_READY = "review.ready"
    REVIEW_FAILED = "review.failed"
    REVIEW_CORRUPT = "review.corrupt"
    REVIEW_EXPIRED = "review.expired"
    REVIEW_VERIFIED = "review.verified"
    REVIEW_DOWNLOADED = "review.downloaded"
    REVIEW_CLEANUP = "review.cleanup"
    REVIEW_DENIED = "review.denied"
    PROPOSAL_IMPORT_ENABLED = "proposal.import_enabled"
    PROPOSAL_IMPORT_DISABLED = "proposal.import_disabled"
    PROPOSAL_IMPORTED = "proposal.imported"
    PROPOSAL_VALIDATING = "proposal.validating"
    PROPOSAL_VALIDATED = "proposal.validated"
    PROPOSAL_REJECTED = "proposal.rejected"
    PROPOSAL_REVIEWED = "proposal.reviewed"
    PROPOSAL_CHANGE_REQUEST_CREATED = "proposal.change_request_created"
    PROPOSAL_IMPLEMENTED = "proposal.implemented"
    PROPOSAL_BACKTESTED = "proposal.backtested"
    PROPOSAL_PAPER_VALIDATED = "proposal.paper_validated"
    PROPOSAL_CLOSED = "proposal.closed"
    PROPOSAL_CLEANUP = "proposal.cleanup"
    PROPOSAL_DENIED = "proposal.denied"
