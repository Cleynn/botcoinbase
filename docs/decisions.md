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
| RG-0: redacted host baseline | **NOT SUPPLIED** | Deployment claims prohibited; Phase 1 code is written but not deployed. |
| RG-0: second host | **WITHDRAWN by owner** (DEC-006) | Not implemented, not planned. |

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
- Host baseline (SSH key-only, no root login, default-deny firewall, IPv6 state, unattended upgrades,
  CAA, chrony): **NOT SUPPLIED**. The second-host part is superseded by DEC-006.
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

### DEC-006: No second host (owner amendment, 2026-09-29)
- Owner: a second host does not exist and **must not be implemented or planned**.
- Baseline items superseded or unsatisfiable, and NOT replaced by any alternative here: BI-33; RG-0 second-host
  part; AS-S5; Phase 2.7 pull backups, exported audit rows, hourly anchors, second-host verifier/watcher; SD-3;
  RQ-1; B-3; alerts AL-07, AL-38; `ctl status` notification path; second-host parts of the restore drill and IR-2, IR-9, IR-12.
- Consequences (recorded, not mitigated): audit integrity would be tamper-evident within one database only
  (RR-2 worsens); no independent watcher or notifier; host loss means local data loss.
- Live trading stays blocked: live blockers 14, 21 and 22 remain true and cannot be cleared without a new decision.
- Security-invariant deviation without an independent reviewer is prohibited (SD-6); the owner authored this amendment.
- review: owner instruction (no independent review)

### DEC-007: Phase 1 implemented from the original implementation prompt (2026-09-29)
- Owner supplied the original Phase 1 prompt; with DEC-000 unacknowledged, that prompt and the Master Contract
  govern Phase 1 where they differ from Baseline 2.0 (tree, Redis, secrets handling).
- Consequences: file layout follows the prompt (`app/`, `config/*.yaml`, `scripts/`, `infra/`) not baseline 3.5.
  Redis and PostgreSQL run as containers on an internal network; the app does not use either in Phase 1.
- Extra files beyond the prompt's allowed list: `app/web/static/js/htmx.min.js` (HTMX 2.0.11 from the npm registry,
  integrity verified, sha256 `d6fdc75f...f717`, needed because HTMX is mandatory and CDNs are banned); `uv.lock`
  and `.venv` handling via `uv`; `tests/pending/` (parked Phase 1.0 skeletons, not collected); baseline-derived docs
  from Phase 1.0 kept unchanged.
- `alembic.ini` not created: no migrations exist in Phase 1.
- Secrets travel as `.env` variables (0600) rather than baseline file secrets (T12 accepted).
- PAPER mode is refused in production during Phase 1 (stricter than the prompt; baseline CI-6).
- The dashboard shell is unauthenticated in Phase 1 by design (threat-model T9); it shows no data.
- review: SELF (non-security deviations)

### DEC-008: Verification limits of the Phase 1 build
- Verified with real output: lint, typecheck, 91 tests, Compose syntax, app start and loopback health probe.
- Not verified: container start (no Docker daemon), Dockerfile build, `caddy validate`, Caddy log redaction syntax,
  DNS/TLS/firewall, base-image digests.
- review: SELF

### DEC-009: Phase 2 implemented from the Phase 2 prompt (2026-09-29)
- Scope: local ADMIN bootstrap, Argon2id, opaque server-side sessions, ADMIN/VIEWER, CSRF, login throttling, audit, reauthentication helper, authenticated dashboard/security/audit pages. Nothing that touches bot, exchange, pair or configuration state.
- **Deviations from Baseline 2.0 (non-security-invariant, self-review):**
  - The baseline's SQL-function privilege model (actor from `session_user`, DB-verified sessions and challenges, `td_fn` owner) is **not** implemented. Enforcement is application-level plus least-privilege grants (`td_app` cannot create users/change roles/alter audit rows; `td_ctl` cannot touch audit history or login attempts), CHECK constraints and append-only triggers. Residual risk RR-1 (a compromised web process can act as any signed-in user within its grants) is unchanged; it remains a live blocker.
  - Time is the application clock (injectable), not `clock_timestamp()`.
  - Migrations are plain SQL files run by `python -m app.storage.database` (no Alembic, so no `alembic.ini`); a schema guard refuses older and newer schemas; `*.down.sql` exists for development only.
  - psycopg without a pool (one short-lived connection per transaction).
  - Single-step reauthentication (confirm password, then act within 120 s, single use) instead of request/confirm challenge rows.
  - Redis is still not used (sessions, throttling and audit live in PostgreSQL).
- **Files beyond the Phase 2 allowed list** (needed to wire the feature; each minimal): `pyproject.toml`, `uv.lock` (argon2-cffi, psycopg, python-multipart; pgserver for tests), `app/config.py` (auth/database/cookie/trusted-proxy settings, production rules), `app/api/app.py`, `app/api/health.py`, `app/domain/__init__.py`, `app/storage/__init__.py`, `config/base.yaml`, `docker-compose.yml`, `Dockerfile`, `.env.example`, `scripts/verify_security_config.py`, `app/web/static/css/app.css`, `docs/architecture.md`, `docs/threat-model.md`, `app/storage/migrations/*.down.sql`, `tests/conftest.py`, `tests/integration/test_app_starts.py`, `tests/integration/test_storage_audit.py`, `tests/security/test_bootstrap_cli.py`, `tests/security/test_no_public_internal_ports.py`, `tests/security/test_no_secret_leakage.py`, `tests/unit/test_config.py`.
- Two restricted database roles plus a one-shot `migrate` service: only `migrate` sees the owner password; the app gets `td_app`; the host CLI (`ctl`, profile `ops`) gets `td_ctl`.
- Session cookies are browser-session cookies (no `Max-Age`); expiry is enforced server-side.
- `Referrer-Policy: same-origin`, not `no-referrer` (found in a real browser: `no-referrer` makes browsers send `Origin: null` on same-origin form posts, which the CSRF check must refuse).
- VIEWER provisioning has no allowed path yet (create_admin is ADMIN-only by requirement); tests create VIEWERs directly in the database.
- review: SELF (non-security deviations). **No independent security review has been performed.**

### DEC-010: Phase 2 verification record
- Real output: ruff, mypy --strict (55 files), 444 tests against real PostgreSQL 16, Compose syntax, and 34/34 checks in real Chromium (login, cookie flags, cross-site CSRF attempt, rotation, logout/back, no-JS, 375 px, CSP console).
- Not verified: container start and image build (no Docker daemon), `caddy validate`, Caddy forwarding, DNS/TLS/firewall, base-image digests.
- review: SELF

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
