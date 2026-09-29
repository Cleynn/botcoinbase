"""View models for the proposal pages. Fixed sentences only; proposal text is data.

Everything that came from a proposal is passed to the templates as a plain string and rendered with
autoescaping inside `<pre>` blocks. Links are built from validated identifiers, never from text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

from app.proposals import policy, risk, schema
from app.proposals.service import (
    ATTEST_KINDS,
    CHANGE_TYPES,
    CLOSE_REASONS,
    PAPER_MIN_SPAN_DAYS,
    PHRASE_ENABLE,
    PHRASE_IMPORT,
    Detail,
    Overview,
    phrase_change_request,
)

LABEL: Final = "UNTRUSTED ADVISORY INPUT"
NOTICE: Final = (
    "This proposal came from outside the system. It is advisory only: it is never executed, "
    "evaluated or applied, and approving it only records a manual change request. Any change "
    "happens later as a separate, reviewed project change."
)

RULE_TEXT: Final[dict[str, str]] = {
    **policy.RULE_TEXT,
    "LINK_PACKAGE_UNKNOWN": "the linked review package does not exist",
    "LINK_PACKAGE_HASH_MISMATCH": "the linked review package checksum does not match",
    "LINK_PACKAGE_STATE": "the linked review package is not READY or EXPIRED",
    "EVIDENCE_NOT_IN_PACKAGE": "an evidence file is not part of the linked package",
    "EVIDENCE_UNKNOWN_ID": "an evidence report, snapshot or backtest run does not exist",
    "UNKNOWN_FIELD": "the document has a field outside the schema",
    "MISSING_FIELD": "a required field is missing",
    "BAD_TYPE": "a field has the wrong type",
    "BAD_VALUE": "a field has an invalid value",
    "BAD_VERSION": "proposal_version must be 1",
    "TOO_LONG": "a field is longer than allowed",
    "TOO_SHORT": "a field is shorter than required",
    "TOO_MANY": "a list has too many items",
    "TOO_FEW": "a list has too few items",
    "BAD_EVIDENCE_REFERENCE": "an evidence reference is not an allowed form",
    "DUPLICATE_EVIDENCE_REFERENCE": "evidence references repeat",
    "CONTROL_CHARACTERS": "control characters are not allowed",
    "HIDDEN_CHARACTERS": "hidden or formatting characters are not allowed",
    "JSON_INVALID": "the file is not valid JSON",
    "JSON_TOO_DEEP": "the JSON is nested too deeply",
    "JSON_DUPLICATE_KEY": "the JSON repeats a key",
    "JSON_NON_FINITE_NUMBER": "the JSON contains NaN or Infinity",
    "NOT_AN_OBJECT": "the JSON is not a single object",
    "FILE_MISSING": "the stored file is missing",
    "FILE_CHECKSUM_MISMATCH": "the stored file changed after import",
    "VALIDATOR_ERROR": "the validator could not process the file",
    "NOT_UTF8": "the file is not UTF-8",
    "NOT_A_JSON_OBJECT": "the file is not a JSON object",
    "TOO_LARGE": "the file is too large",
}
INPUT_TEXT: Final[dict[str, str]] = {
    "MIME_MISSING": "The upload has no declared type.",
    "MIME_NOT_ALLOWED": "Only text/plain and application/json are accepted.",
    "MIME_PARAMETER_NOT_ALLOWED": "Only the utf-8 charset is accepted.",
    "EXTENSION_NOT_ALLOWED": "Only .json or .txt files are accepted.",
    "FILENAME_NOT_ALLOWED": "The file name is not acceptable.",
    "TOO_LARGE": "The proposal is larger than the 128 KiB limit.",
    "EMPTY": "The proposal is empty.",
    "NOT_UTF8": "The proposal is not UTF-8 text.",
    "BINARY_CONTENT": "The proposal contains binary data.",
    "CONTROL_CHARACTERS": "The proposal contains control characters.",
    "HIDDEN_CHARACTERS": "The proposal contains hidden or formatting characters.",
    "BYTE_ORDER_MARK_NOT_ALLOWED": "Byte-order marks are not accepted.",
    "NOT_A_JSON_OBJECT": "The proposal must be one JSON object.",
    "MARKUP_NOT_ALLOWED": "HTML and XML are not accepted.",
    "YAML_NOT_ALLOWED": "YAML is not accepted.",
    "SCRIPT_NOT_ALLOWED": "Scripts are not accepted.",
    "ARCHIVE_FILE": "Archives are not accepted.",
    "ARCHIVE_OR_OFFICE_FILE": "Archives and Office documents are not accepted.",
    "PDF_FILE": "PDF files are not accepted.",
    "OFFICE_FILE": "Office documents are not accepted.",
    "IMAGE_FILE": "Images are not accepted.",
    "BINARY_FILE": "Binary files are not accepted.",
    "EXECUTABLE_FILE": "Executables are not accepted.",
    "SCRIPT_FILE": "Shell scripts are not accepted.",
    "SOURCE_NOT_ALLOWED": "Send either one file or pasted text.",
    "IMPORT_DISABLED": "Proposal import is disabled.",
    "RATE_LIMIT_HOUR": "Ten proposals were imported in the last hour.",
    "RATE_LIMIT_DAY": "Twenty proposals were imported in the last day.",
    "STORAGE_LIMIT": "The proposal storage limit was reached.",
    "DUPLICATE": "The same proposal is already imported.",
    "DATABASE_REFUSED": "The database refused the import.",
}
STATE_STEPS: Final[dict[str, str]] = {
    "IMPORTED": "Waiting for the host validator. On the host run: make proposal-validate",
    "VALIDATING": "The host validator is processing it.",
    "VALIDATED": "Read it as untrusted text. If it is worth pursuing, record review notes.",
    "REJECTED": "Read the reasons. Fix the proposal and import a new one, or close this record.",
    "REVIEWED": "To pursue it, create a manual change request (full confirmation), or close it.",
    "CHANGE_REQUEST_CREATED": (
        "Implement it as a separate, reviewed code or configuration change in the project "
        "repository (normal review, tests and release). Then attest the release reference here."
    ),
    "IMPLEMENTED": (
        "Run a backtest of the implementation on a frozen snapshot, then attest the report ids."
    ),
    "BACKTESTED": (
        f"Run paper trading for at least {PAPER_MIN_SPAN_DAYS} days, "
        "then attest the paper report ids."
    ),
    "PAPER_VALIDATED": "Close the proposal when the follow-up work is finished.",
    "CLOSED": "Closed. The record and its history are kept.",
}
MESSAGES: Final[dict[str, tuple[str, str]]] = {
    "proposal_import_enabled": ("success", "Proposal import is enabled."),
    "proposal_import_disabled": ("success", "Proposal import is disabled."),
    "proposal_imported": ("success", "Proposal stored. It is untrusted and not yet validated."),
    "proposal_reviewed": ("success", "Review notes recorded."),
    "proposal_change_request": (
        "success",
        "Manual change request recorded. Nothing was changed anywhere.",
    ),
    "proposal_implemented": ("success", "Implementation attested."),
    "proposal_backtested": ("success", "Backtest attested."),
    "proposal_paper_validated": ("success", "Paper validation attested."),
    "proposal_closed": ("success", "Proposal closed."),
    "reauth_ok": ("success", "Password confirmed. It can authorise one action within the window."),
    "reauth_invalid": ("error", "The password is incorrect."),
    "throttled": ("error", "Too many attempts. Try again later."),
    "phrase_mismatch": ("error", "The typed phrase did not match exactly. Nothing was changed."),
    "reauth_required": ("error", "Confirm your password first. Nothing was changed."),
    "proposal_conflict": ("error", "That is not possible in the current state."),
    "proposal_disabled": ("error", "Proposal import is disabled."),
    "proposal_limit": ("error", "A limit refused the request. Nothing was changed."),
    "proposal_invalid": ("error", "The request was not valid. Nothing was changed."),
    "proposal_not_allowed": ("error", "That step is not available in the current state."),
    "not_found": ("error", "The request could not be completed."),
}
_ATTEST_PATH: Final = {
    "IMPLEMENTED": "implemented",
    "BACKTESTED": "backtested",
    "PAPER_VALIDATED": "paper-validated",
}
ATTEST_FROM_PATH: Final = {v: k for k, v in _ATTEST_PATH.items()}


def flash(code: str | None) -> tuple[str | None, str | None]:
    kind, text = MESSAGES.get(code or "", (None, None))
    return kind, text


def rule_label(rule: str) -> str:
    return RULE_TEXT.get(rule.split(":")[0], "refused by validation")


def input_text(code: str) -> str:
    return INPUT_TEXT.get(code, "The proposal was refused.")


def _ts(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "Not available"


@dataclass(frozen=True)
class RowView:
    id: str
    state: str
    category: str
    ref: str
    risk: str
    imported: str


@dataclass(frozen=True)
class OverviewView:
    label: str
    notice: str
    enabled: bool
    updated: str
    rows: tuple[RowView, ...]
    counts: tuple[tuple[str, int], ...]
    blockers: tuple[str, ...]
    blocker_text: tuple[str, ...]
    message_kind: str | None
    message_text: str | None


def build_overview(
    o: Overview, message: str | None, extra_error: str | None = None
) -> OverviewView:
    kind, text = flash(message)
    if extra_error:
        kind, text = "error", input_text(extra_error)
    return OverviewView(
        LABEL,
        NOTICE,
        o.settings.import_enabled,
        _ts(o.settings.updated_at),
        tuple(
            RowView(
                str(p.id),
                p.state,
                p.category or "-",
                p.proposal_ref or "-",
                (p.risk_assessment or {}).get("level", "-"),
                _ts(p.imported_at),
            )
            for p in o.proposals
        ),
        tuple(sorted(o.counts.items())),
        o.import_blockers,
        tuple(input_text(b) for b in o.import_blockers),
        kind,
        text,
    )


@dataclass(frozen=True)
class Link:
    text: str
    href: str | None


@dataclass(frozen=True)
class DetailView:
    label: str
    notice: str
    id: str
    state: str
    category: str
    ref: str
    size: str
    sha: str
    mime: str
    imported: str
    package_id: str | None
    package_sha: str | None
    risk: dict[str, object] | None
    risk_factors: tuple[str, ...]
    findings: tuple[tuple[str, str, str, str], ...]
    reject_rules: tuple[tuple[str, str], ...]
    fields: tuple[tuple[str, str], ...]
    assumptions: tuple[str, ...]
    evidence: tuple[Link, ...]
    history: tuple[tuple[str, str, str], ...]
    review_notes: str
    change_request: tuple[str, str, str, bool, str] | None
    attestations: tuple[tuple[str, str, str], ...]
    next_step: str
    actions: tuple[str, ...]
    content_removed: bool
    close_reasons: tuple[str, ...]
    message_kind: str | None
    message_text: str | None


def _evidence(refs: list[str], package_id: str | None) -> tuple[Link, ...]:
    out: list[Link] = []
    for ref in refs:
        ident = schema.EVIDENCE_ID.fullmatch(ref)
        if schema.EVIDENCE_PATH.fullmatch(ref):
            out.append(Link(ref, f"/review/packages/{package_id}" if package_id else None))
        elif ident and ident.group("kind") == "report":
            out.append(Link(ref, f"/reports/{ident.group('id')}"))
        else:
            out.append(Link(ref, None))
    return tuple(out)


def _actions(d: Detail) -> tuple[str, ...]:
    state = d.proposal.state
    acts = {
        "VALIDATED": ("review", "close-unavailable"),
        "REJECTED": ("close",),
        "REVIEWED": ("change-request", "close"),
        "CHANGE_REQUEST_CREATED": ("attest-implemented", "close"),
        "IMPLEMENTED": ("attest-backtested",),
        "BACKTESTED": ("attest-paper-validated",),
        "PAPER_VALIDATED": ("close",),
    }.get(state, ())
    return tuple(a for a in acts if a != "close-unavailable")


def build_detail(d: Detail, message: str | None) -> DetailView:
    p = d.proposal
    kind, text = flash(message)
    parsed = p.parsed or {}
    field_names = (
        "summary", "suggested_change", "expected_benefit", "risk_tradeoffs",
        "required_validation", "rollback_plan",
    )  # fmt: skip
    package_id = str(p.linked_package_id) if p.linked_package_id else None
    cr = d.change_request
    assessment = p.risk_assessment
    return DetailView(
        label=LABEL,
        notice=NOTICE,
        id=str(p.id),
        state=p.state,
        category=p.category or "-",
        ref=p.proposal_ref or "-",
        size=f"{p.size_bytes} bytes",
        sha=p.sha256,
        mime=p.declared_mime,
        imported=_ts(p.imported_at),
        package_id=package_id,
        package_sha=p.linked_package_sha256,
        risk=assessment,
        risk_factors=tuple(
            f"{f}: {risk.FACTOR_TEXT.get(f, 'factor')}"
            for f in (assessment or {}).get("factors", [])
        ),
        findings=tuple(
            (f["rule"], RULE_TEXT.get(f["rule"], "policy rule"), f["field"], f.get("excerpt", ""))
            for f in (p.findings or [])
        ),
        reject_rules=tuple((r, rule_label(r)) for r in (p.reject_rules or ())),
        fields=tuple((n, str(parsed[n])) for n in field_names if n in parsed),
        assumptions=tuple(str(a) for a in parsed.get("assumptions", [])),
        evidence=_evidence([str(e) for e in parsed.get("evidence_references", [])], package_id),
        history=tuple((h.state_after, h.actor_class, _ts(h.occurred_at)) for h in d.history),
        review_notes=p.review_notes or "",
        change_request=(
            (
                f"CR-{cr.seq:06d}",
                cr.change_type,
                cr.impact_assessment,
                cr.ceilings_unaffected,
                _ts(cr.created_at),
            )
            if cr
            else None
        ),
        attestations=tuple(
            (k, a.reference or ", ".join(str(i) for i in (a.report_ids or ())), _ts(a.attested_at))
            for k, a in sorted(d.attestations.items())
        ),
        next_step=STATE_STEPS.get(p.state, ""),
        actions=_actions(d),
        content_removed=p.content_removed_at is not None,
        close_reasons=CLOSE_REASONS,
        message_kind=kind,
        message_text=text,
    )


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str  # text | textarea | select | checkbox | file
    options: tuple[str, ...] = ()
    hint: str = ""
    max_length: int = 0
    value: str = ""


@dataclass(frozen=True)
class ConfirmView:
    label: str
    notice: str
    title: str
    action_path: str
    back_href: str
    summary: tuple[str, ...]
    phrase: str | None
    fields: tuple[FieldSpec, ...]
    multipart: bool
    reauth_active: bool
    message_kind: str | None
    message_text: str | None
    submit_label: str = "Confirm"
    extra: dict[str, str] = field(default_factory=dict)


def _confirm(
    title: str,
    prefix: str,
    back: str,
    summary: tuple[str, ...],
    phrase: str | None,
    fields: tuple[FieldSpec, ...],
    reauth_active: bool,
    message: str | None,
    *,
    multipart: bool = False,
    input_error: str | None = None,
) -> ConfirmView:
    kind, text = flash(message)
    if input_error:
        kind, text = "error", input_text(input_error)
    return ConfirmView(
        LABEL,
        NOTICE,
        title,
        prefix,
        back,
        summary,
        phrase,
        fields,
        multipart,
        reauth_active,
        kind,
        text,
        title,
    )


ENABLE_PREFIX: Final = "/review/proposals/import-enable"
IMPORT_PREFIX: Final = "/review/proposals/import"


def confirm_enable(reauth_active: bool, message: str | None) -> ConfirmView:
    return _confirm(
        "Enable untrusted proposal import",
        ENABLE_PREFIX,
        "/review/proposals",
        (
            "Enabling lets an ADMIN import proposals written by an LLM. Imported text is "
            "untrusted, is validated on the host and can never be applied automatically.",
        ),
        PHRASE_ENABLE,
        (),
        reauth_active,
        message,
    )


def confirm_import(
    reauth_active: bool, message: str | None, error: str | None = None
) -> ConfirmView:
    return _confirm(
        "Import an untrusted LLM proposal",
        IMPORT_PREFIX,
        "/review/proposals",
        (
            "Accepted: one UTF-8 JSON object, up to 128 KiB, as a .json or .txt file "
            "(text/plain or application/json) or pasted text. Everything else is refused.",
            "The proposal must name a review package (id and SHA-256) and follow the schema.",
            "Import only stores it. The host validator parses and checks it afterwards.",
        ),
        PHRASE_IMPORT,
        (
            FieldSpec("file", "Proposal file (.json or .txt)", "file"),
            FieldSpec("text", "Or paste the proposal JSON", "textarea", max_length=131072),
        ),
        reauth_active,
        message,
        multipart=True,
        input_error=error,
    )


def confirm_change_request(
    proposal_id: UUID, reauth_active: bool, message: str | None
) -> ConfirmView:
    return _confirm(
        "Create a manual change request",
        f"/review/proposals/{proposal_id}/change-request",
        f"/review/proposals/{proposal_id}",
        (
            "This records a tracked MANUAL change request. It changes no code, configuration, "
            "pair, order or setting. Implementation is a separate reviewed project change.",
        ),
        phrase_change_request(proposal_id),
        (
            FieldSpec("change_type", "Change type", "select", options=CHANGE_TYPES),
            FieldSpec(
                "impact_assessment",
                "Impact assessment (20 to 2000 characters)",
                "textarea",
                max_length=2000,
                hint="What could this affect, and how will it be validated?",
            ),
            FieldSpec(
                "ceilings_unaffected",
                "For a PARAMETER_CHANGE: I attest that the hard "
                "capital ceilings (50 / 15 / 35 USDC) are unaffected",
                "checkbox",
            ),
        ),
        reauth_active,
        message,
    )


def confirm_attest(
    proposal_id: UUID, kind: str, reauth_active: bool, message: str | None
) -> ConfirmView:
    path = _ATTEST_PATH[kind]
    if kind == "IMPLEMENTED":
        fields = (
            FieldSpec(
                "reference",
                "Release reference (tag or commit, 3 to 64 characters)",
                "text",
                max_length=64,
            ),
        )
        summary = (
            "You attest that the change was implemented and released through the normal "
            "project process. Nothing is verified by the system.",
        )
    else:
        what = "backtest" if kind == "BACKTESTED" else "paper"
        fields = (
            FieldSpec(
                "report_ids",
                f"{what.title()} report ids (one per line, up to 5)",
                "textarea",
                max_length=400,
            ),
        )
        summary = (
            f"Cite existing {what} reports created after the previous step. "
            "The system checks that they exist and are the right kind.",
        )
    return _confirm(
        f"Attest: {kind.replace('_', ' ').lower()}",
        f"/review/proposals/{proposal_id}/attest/{path}",
        f"/review/proposals/{proposal_id}",
        summary,
        None,
        fields,
        reauth_active,
        message,
    )


__all__ = [
    "ATTEST_FROM_PATH",
    "ATTEST_KINDS",
    "LABEL",
    "NOTICE",
]
