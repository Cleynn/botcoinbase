# State machines

Status: pair machine **implemented (Phase 4)**; bot, package and proposal machines are not built yet. DEC-000 is
unacknowledged. Source: `baseline/TRADINGDOTS_HANDOFF.md` section 4.2 as amended by DEC-013.

The pair machine and its complete transition table are in `docs/pair-management.md`; the code table is
`app/domain/pairs.py` (`all_transitions()`), seeded into `allowed_transitions` by migration 0002 and enforced by a trigger.

## Paper trader phases (Phase 5)
`IDLE -> ACTIVE` (a grid was accepted) `-> STOPPED` (close beyond the band by the breakout buffer; orders cancelled, inventory kept) or `-> HALTED` (drawdown past the stop ratio; cleared only by `paper start --acknowledge-halt`). The paper session is `PAUSED <-> RUNNING`; the database allows paper orders only while it is RUNNING and only for the PAPER_ACTIVE pair. Order states: OPEN -> FILLED | CANCELLED | REJECTED (closed orders never change).

## Review package lifecycle (Phase 6)
Feature flag: DISABLED (default) <-> ENABLED, ADMIN full chain both ways. Package: `REQUESTED -> GENERATING -> READY | FAILED`; `READY -> CORRUPT` (web or host, on a failed verification) or `EXPIRED` (host, retention); `CORRUPT | FAILED -> EXPIRED`. `EXPIRED` is terminal and keeps a tombstone row after the content is removed. The database trigger allows only these transitions, only for the listed actor class (web may only do `READY -> CORRUPT`), keeps the request fields immutable, forbids DELETE/TRUNCATE, and enforces one waiting-or-building package, at most 10 retained, at most 3 requests per hour, and that requests exist only while the feature is enabled.

## Proposal lifecycle (Phase 7)
`IMPORTED -> VALIDATING -> VALIDATED | REJECTED -> REVIEWED -> CHANGE_REQUEST_CREATED -> IMPLEMENTED -> BACKTESTED -> PAPER_VALIDATED -> CLOSED`; also `REJECTED | REVIEWED | CHANGE_REQUEST_CREATED -> CLOSED`. HOST (`td_ctl`) performs IMPORTED -> VALIDATING -> VALIDATED | REJECTED only; WEB (`td_app`, ADMIN) performs the rest. Database triggers enforce actor class, immutable content fields, write-once validation/review/close fields, required history and record rows (`change_requests`, `proposal_attestations`), rate and storage limits and "insert only while import is enabled". No DELETE, no TRUNCATE. See `docs/proposals.md`.
