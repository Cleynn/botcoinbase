# State machines

Status: pair machine **implemented (Phase 4)**; bot, package and proposal machines are not built yet. DEC-000 is
unacknowledged. Source: `baseline/TRADINGDOTS_HANDOFF.md` section 4.2 as amended by DEC-013.

The pair machine and its complete transition table are in `docs/pair-management.md`; the code table is
`app/domain/pairs.py` (`all_transitions()`), seeded into `allowed_transitions` by migration 0002 and enforced by a trigger.

## Paper trader phases (Phase 5)
`IDLE -> ACTIVE` (a grid was accepted) `-> STOPPED` (close beyond the band by the breakout buffer; orders cancelled, inventory kept) or `-> HALTED` (drawdown past the stop ratio; cleared only by `paper start --acknowledge-halt`). The paper session is `PAUSED <-> RUNNING`; the database allows paper orders only while it is RUNNING and only for the PAPER_ACTIVE pair. Order states: OPEN -> FILLED | CANCELLED | REJECTED (closed orders never change).
