# State machines

Status: pair machine **implemented (Phase 4)**; bot, package and proposal machines are not built yet. DEC-000 is
unacknowledged. Source: `baseline/TRADINGDOTS_HANDOFF.md` section 4.2 as amended by DEC-013.

The pair machine and its complete transition table are in `docs/pair-management.md`; the code table is
`app/domain/pairs.py` (`all_transitions()`), seeded into `allowed_transitions` by migration 0002 and enforced by a trigger.
