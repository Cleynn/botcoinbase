# TradingDots decisions log

Binding record per `baseline/TRADINGDOTS_HANDOFF.md` (Part 0, sections 0.1 and 0.3).
Precedence: Master Contract > Baseline 2.0 > planning history. The stricter rule wins.
Every entry carries `review: <name|SELF|NONE>`. Security-invariant (BI-01..BI-24, live blockers)
deviations are **prohibited** while no independent reviewer exists (SD-6).

## Start conditions (section 0.3), recorded 2026-09-29

| Condition | Status | Effect |
|---|---|---|
| DEC-000 owner acknowledgment of CI-1..CI-15 | **NOT DONE**. Owner chose the safe default | Baseline is **not binding**. Phase 1.0 only. |
| Independent Security Engineer + Code Reviewer re-review of Baseline 2.0 | **NOT DONE** | Security-invariant deviations prohibited. |
| RG-0 evidence (redacted host baseline + second host) | **NOT SUPPLIED** | No Docker, application or database code. |

## Decisions

### DEC-000: Contract interpretations CI-1..CI-15
- Status: **PENDING (not acknowledged)**. Owner instruction 2026-09-29: "DEC-000 default".
- Consequence: only the documentation skeleton, this file and failing test skeletons exist.
- Unblocks when the owner acknowledges CI-1..CI-15 (or names amendments) in a new entry.
- review: NONE

### DEC-001: Independent re-review of the baseline
- Status: **NOT DONE** (owner statement 2026-09-29).
- Consequence: SD-6 applies: no deviation touching security invariants; other deviations need `review: SELF`.
- review: NONE

### DEC-002: RG-0 evidence
- Status: **NOT SUPPLIED**. Required: redacted host baseline (SSH key-only, no root login,
  default-deny firewall, IPv6 state, unattended upgrades, CAA, chrony) and confirmation
  that a second host exists for pull backups, anchors and status checks (AS-S5, BI-33).
- review: NONE

### DEC-003: Handoff stored in the repository
- The handoff is committed as `baseline/TRADINGDOTS_HANDOFF.md` (outside `docs/`, which must hold
  exactly 14 files) so the baseline survives ephemeral sessions. It is the source for all seeding.
- review: SELF (non-security)

### DEC-004: Coinbase documentation verification status (2026-09-29)
- The sandbox egress proxy denied `docs.cdp.coinbase.com` and `api.coinbase.com` (HTTP 403 on CONNECT).
  No full crawl was possible. Only web-search snippets were seen. See `docs/exchange-contract.md`.
- Nothing in the baseline was changed on the basis of snippets. Items are recorded as open.
- review: SELF (non-security)

### DEC-005: Toolchain in the authoring environment
- Authoring environment has Python 3.11; the baseline targets 3.12+. Test skeletons use only
  syntax valid on both. No dependency has been pinned (Phase 1.1).
- review: SELF (non-security)

## Safe defaults adopted from the baseline (section 2.7), pending DEC-000
SD-1 separate `intake` container; SD-2 Grafana second layer in Caddy; SD-3 second host pulls backups
and anchors; SD-4 audited paper dust write-off; SD-5 step-up beyond password deferred to Phase 11;
SD-6 deviation-review rule; SD-7 DISCOVERED stored on the product; RQ-1 second host as watcher;
RQ-2 activation requires bot PAUSED; RQ-3 fees as a `runtime_configs` kind; BQ-2 proposals as DB
records; BQ-3 at most 24 SQL functions and a NOLOGIN owner role; BQ-4 CLI-originated kill resets only
from the CLI; B-1 runner-agnostic `make ci`; B-3 second-host watcher; B-4 5-minute candles over 90
days; B-5 30-day paper soak; B-6 no credentials before Phase 11; B-7 staged HSTS, TCP only; B-8
Grafana second layer; B-9 approve proposed package states and typed phrases; DX-1 sandbox fixtures
recorded off the VPS; DX-2 ES256; DX-3 fee review interval 30 days, stress maker fee 0.60% per side.

## Deviations log
| ID | Date | Baseline item | Change | review |
|---|---|---|---|---|
| (none) | | | | |
