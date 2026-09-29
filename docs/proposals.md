# Imported LLM proposals (Phase 7)

Status: implemented, **disabled by default**. Everything imported is labelled **UNTRUSTED ADVISORY INPUT**.
Decisions: DEC-019 (design), DEC-020 (verification). DEC-000 is unacknowledged; live trading stays BLOCKED.

A proposal is a JSON document, written by a person with an AI assistant from a review package (Phase 6),
that an ADMIN imports by hand. The platform parses it strictly, screens it against policy, shows it,
and lets an ADMIN record a review and open a **manual change request**. **Nothing is ever applied.** No
code, configuration, order, pair or system state is changed by any proposal action.

## What the feature can and cannot do
| Can | Cannot |
|---|---|
| Store the bytes (opaque, generated name, outside the web root) | Execute, evaluate, template, deserialise or render them raw |
| Validate on the host: JSON, strict schema, package link, policy, risk assessment | Change a pair, order, risk limit, secret, config or code |
| Show escaped text, findings, rule ids and evidence links | Call an LLM or any network service |
| Record a review, a manual change request and three attestations | Approve itself, apply itself or promote anything |

## Enabling and importing (ADMIN only, every step audited)
| Action | Requires |
|---|---|
| Enable import | ADMIN + CSRF + fresh single-use reauth + `ENABLE UNTRUSTED PROPOSAL IMPORT` |
| Import a proposal | ADMIN + CSRF + fresh single-use reauth + `IMPORT UNTRUSTED LLM PROPOSAL`, plus a valid file or pasted text |
| Create the manual change request | ADMIN + CSRF + fresh reauth + `CREATE MANUAL CHANGE REQUEST FOR PROPOSAL <PROPOSAL_ID>` |
| Attest IMPLEMENTED / BACKTESTED / PAPER_VALIDATED | ADMIN + CSRF + fresh reauth (no phrase) |
| Record a review, close, disable import | ADMIN + CSRF only (these only move toward "less happens") |

A wrong phrase, a bad file or a failed check never spends the single-use reauth. The phrase for the change
request contains the proposal id, so a phrase for one proposal cannot approve another.

## Upload rules (checked by the web tier, before anything is stored)
- Exactly one source: one `file` part **or** pasted `text`, plus `csrf_token` and `confirmation`. Any other field, a repeated field, two files, or both sources is refused.
- Declared MIME must be `text/plain` or `application/json` (a `charset=utf-8` parameter is allowed). Extension, if any, must be `.json` or `.txt`; a client file name is never stored or trusted.
- Bytes must be valid UTF-8 and a JSON *object* by sniffing; magic bytes for archives, PDF, Office, images, ELF/PE/Mach-O, gzip and similar are refused, as are markup, YAML and script prefixes. NUL and control characters are refused.
- Limits: 128 KiB per proposal, 400 KiB request body for the upload route only, 10 imports per hour, 20 per day, 200 MB stored. Oversize bodies are cut off by the ASGI body cap; an upload with no session is refused before its body is read.
- CSRF on multipart accepts exactly one file part and only on the upload route; every other route refuses files.
- The web tier never parses the JSON. It stores the bytes as `<uuid>.proposal` (mode 0440, atomic, fsync) in `TD_PROPOSAL_DIR` (default `/proposals`, refused if web-root-like) and inserts an IMPORTED row.

## Strict schema (`proposal_version` 1; parsed only by the host validator)
Fifteen fields, no others: `proposal_version`, `proposal_id`, `linked_review_package_id`, `linked_review_package_sha256`, `category` (strategy, risk_parameters, data_quality, monitoring, execution_quality, operations, documentation, research, other), `summary`, `evidence_references` (1-20), `assumptions` (0-20), `suggested_change`, `expected_benefit`, `risk_tradeoffs`, `required_validation`, `rollback_plan`, `should_remain_no_trade_until_validated` (must be true), `no_profit_guarantee` (must be true).
- Unknown fields, duplicate keys, NaN/Infinity, nesting deeper than 6, wrong types, over-long text, hidden or control characters: REJECTED.
- The linked review package must exist, be READY and match the stated SHA-256. Evidence is either a file path listed in that package (optional `#L<n>`) or `report:`, `snapshot:` or `backtest_run:` plus an existing UUID.

## Policy triage (over-inclusive on purpose)
Text is normalised (NFKC, casefold, confusable letters, punctuation removed) and matched with stem-based verb/noun proximity rules. Any hit REJECTS the proposal and shows the rule id. Rules: `RISK_BYPASS`, `SECRET_CHANGE`, `SECURITY_CONTROL_CHANGE`, `API_ACCESS_CHANGE`, `LIVE_ACTIVATION`, `PAIR_STATE_CHANGE`, `CAPITAL_INCREASE`, `AUTOMATED_ORDER`, `AUTO_APPLICATION`, `PROFIT_GUARANTEE`, `MARKUP_OR_TEMPLATE`, `EXECUTABLE_CONTENT`, `EXTERNAL_REFERENCE`, `NO_TRADE_NOT_PRESERVED`, `PROFIT_DECLARATION_MISSING`.
The only exemption is a plain "no guarantee of profit" disclaimer. A *negated* mention ("never disable the risk limits") is still blocked: a false rejection costs a rewrite, a false acceptance costs trust. Policy is heuristic and cannot be a security boundary by itself: the real boundary is that **no proposal action can change anything**.

## Risk assessment
Deterministic LOW / MEDIUM / HIGH / BLOCKED from category and findings, with a fixed vocabulary of factors and a review depth. It is advisory and never gates anything except that BLOCKED (= REJECTED) proposals cannot be reviewed.

## Lifecycle
`IMPORTED -> VALIDATING -> VALIDATED | REJECTED -> REVIEWED -> CHANGE_REQUEST_CREATED -> IMPLEMENTED -> BACKTESTED -> PAPER_VALIDATED -> CLOSED`
- HOST (`make proposal-validate`): IMPORTED -> VALIDATING -> VALIDATED | REJECTED.
- WEB (ADMIN): VALIDATED -> REVIEWED -> CHANGE_REQUEST_CREATED -> IMPLEMENTED -> BACKTESTED -> PAPER_VALIDATED -> CLOSED; and REJECTED | REVIEWED | CHANGE_REQUEST_CREATED -> CLOSED.
- Enforced by database triggers per actor class (a web session cannot validate; the host cannot review), with immutable content fields, write-once validation/review/close fields, required history rows, append-only `change_requests` and `proposal_attestations`, and no DELETE.
- The change request records a change type (PARAMETER_CHANGE, CODE_CHANGE, CONFIG_RELEASE, DOCUMENTATION, MONITORING, RESEARCH_ONLY), an impact assessment (20-2000 characters) and, for PARAMETER_CHANGE, an attestation that ceilings are unaffected. **It is a record.** The change itself is a separate, ordinary code/config change under the repository's review gates.
- Attestations are human statements checked against evidence: IMPLEMENTED needs a release reference; BACKTESTED needs 1-5 existing BACKTEST/WALK_FORWARD report ids created at or after the change request; PAPER_VALIDATED needs at least two PAPER_DAILY reports, created after the backtest attestation, spanning at least 7 days.

## Pages (one interface, ADMIN only)
`/review/proposals` (status, list, links), `/review/proposals/import/request` (two steps: password, then file or text + phrase), `/review/proposals/{id}` (label, state, findings with plain-language rule text, risk assessment, evidence links, manual next steps, history, forms). Every page carries the label; all proposal text is auto-escaped; there is no inline script, no `|safe`, no raw-file view and no download route. Route list: `docs/api-contracts.md`.

## Operations
`make proposal-validate` (host), `make proposal-cleanup` (removes bytes of CLOSED/REJECTED proposals after 90 days, and orphan/temp files; rows stay), `make proposal-list`. Volume `proposals`: read-write on `app` and `batch` only (`verify_security_config.py` checks it). Metrics (aggregate only): `tradingdots_proposal_import_enabled`, `tradingdots_proposals{state}`, `tradingdots_llm_proposals_total`, `tradingdots_llm_proposal_policy_rejections_total`.

## Rollback
Disable import (ADMIN, one click). Development/test only: `rollback(target, to_version=4)` applies `0005_proposals.down.sql` (drops the five proposal tables and functions; audit events stay; proposal files on disk are not removed and must be deleted by hand). Verified on a real PostgreSQL.

## Fictional example
`docs/examples/proposal-FICTIONAL.json` and `docs/examples/proposal-dashboard-FICTIONAL.md`. Synthetic; no real data.
