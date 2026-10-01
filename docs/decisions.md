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

### DEC-011: Phase 3 monitoring (2026-09-29)
- Implemented from the Phase 3 prompt: Prometheus, Grafana, node-exporter, optional cAdvisor, internal application metrics, recording and alert rules, Grafana provisioning and five dashboards, a monitoring summary on the main dashboard. No external alert transport and no monitoring-to-bot action exists.
- **Metrics honesty (requirement 19):** only metrics with a real source are published (19 catalogued families plus `process_*`/`python_*`). The Master Contract's `tradingdots_bot_*` (except `tradingdots_bot_info`), `tradingdots_pair_*` and `tradingdots_llm_*` metrics are **reserved, not published**; tests enforce this in the catalogue, the live output, the rules and the dashboards. Consequently the Risk and Failsafes dashboard shows *security* failsafes, and Execution and Reconciliation contains only context and an explanation.
- **No alerts for unbuilt components** (baseline used `absent_over_time` for subsystems; that would fire permanently now). They are listed as reserved in `docs/alert-policy.md`.
- **Metrics are served by a separate internal listener** (private bind, allowed scrapers only), not by FastAPI, so the web port and Caddy can never expose them. A bind failure is reported on the dashboard and does not stop the web application. Baseline BI-27 ("web process has no background tasks") is relaxed by one daemon thread serving `/metrics`; it holds no credentials of its own and reads only the cached snapshot.
- **The web tier never queries Prometheus** (baseline BI-27/CI-8). The dashboard summary is computed in-process from the same snapshot; the "Alerts" tile stays `Not available`. VIEWERs see no audit event counts or positions.
- **Grafana:** own admin user `td-admin`; a required unique `GRAFANA_SECRET_KEY` (Grafana's built-in default key is public); alerting, metrics, plugins, sharing and update checks off; dashboards and the single credential-less datasource provisioned read-only. The optional Caddy second layer (baseline SD-2/B-8) is documented, not enabled.
- **cAdvisor** is optional (profile `cadvisor`) and mounts neither the Docker socket nor `/var/lib/docker`; containers are identified by cgroup id. Untested here.
- **node-exporter** runs non-root with read-only host mounts but without host networking, so its network metrics describe its own namespace (documented on the panel).
- Static addresses: `172.29.20.0/24` for `mon_scrape` (app at `.10`), alongside `172.29.10.0/24` for `edge_app`. If they clash with a host network, change Compose and `monitoring.allowed_scrapers`/`trusted_proxies` together.
- **Files beyond the Phase 3 allowed list** (each minimal): `app/config.py` (monitoring block, `TD_METRICS_BIND`, production rules), `app/storage/repositories.py` (a read-only `MonitoringRepository` for aggregate queries), `app/monitoring/` is new as listed, `.env.example` and `scripts/verify_security_config.py` (`GRAFANA_SECRET_KEY`), `pyproject.toml`/`uv.lock` (`prometheus-client`, dev `promql-parser`, `pgserver` earlier; version 0.3.0), `Makefile` (two targets), `docs/architecture.md`, `docs/threat-model.md`, `docs/decisions.md`, `README.md`, and tests: `tests/unit/test_config.py`, `tests/security/test_no_public_internal_ports.py`, `tests/security/test_no_secret_leakage.py`, `tests/security/test_authorization_boundaries.py`, plus new `tests/unit/test_metrics.py` etc. as listed.
- review: SELF (non-security deviations). **No independent security review has been performed.**

### DEC-012: Phase 3 verification record
- Real tools: `promtool` 2.53.0 (config, rules, 7 rule unit tests) and a real Prometheus 2.53.0 scraping the real application (PostgreSQL 16) and node_exporter 1.8.1, evaluating all 28 rules and executing every dashboard query; behaviour of the disabled lifecycle/admin APIs confirmed against the real binary. Grafana could **not** be run (download host blocked).
- Not verified: container start and health checks for the new services, Caddy proxying Grafana, Grafana first start and rendering, cAdvisor, resource estimates, host mounts on the VPS.
- review: SELF

### DEC-013: Phase 4 pairs (2026-09-29)
Implemented: public product discovery, pair policy, 14-check validation with persisted reasons, lifecycle with database-enforced transitions, Pairs UI/API, disable/archive/re-enable/activate/resume full chain, pair audit events and metrics, host CLI, egress allowlist proxy. Deviations from the baseline (all stricter or smaller in scope, none touches a security invariant):
- **No `td_worker`.** Discovery, seeding, validation and expiry run from the host CLI as `td_ctl` until the worker exists (baseline 4.1). `td_ctl` is the only role that can write products, metadata and validation runs; `td_app` cannot. Actor class (WEB/HOST) is derived from `current_user` inside the trigger.
- **Egress proxy included** (baseline 4.1) as a small Python CONNECT allowlist (`api.coinbase.com:443` only, public addresses only), `discovery` profile only. `scripts/verify_security_config.py` now permits exactly one non-Caddy service on a non-internal network: `egress-proxy` on `egress_ext`.
- **Not created** (out of scope for Phase 4): ledger, intent and attempt tables, `work_requests`, `runtime_configs`, package and proposal tables. Fees are therefore operator-attested in `config/pair-policy.yaml` (RQ-3 later moves them to a runtime config kind).
- **Activation is wired but refused** by the default runtime gate (`BOT_STATE_UNAVAILABLE`, `MODE_NOT_PAPER`): the baseline requires a PAUSED bot in PAPER mode. Tighter than baseline: activation is also refused while any other pair is PAUSED, and disable/archive of a previously active pair is refused until reconciliation can show it clean (it cannot yet).
- **Confirmation challenge:** the baseline binds a reauth challenge to (session, action, target, version). Phase 2's single-use, 120 s session reauthentication is reused; the target and version are bound by the typed phrase (which contains the product id) and the `version` form field (stale views are refused).
- **LIVE_ELIGIBLE / LIVE_ACTIVE** appear in the UI as blocked lifecycle steps only; no enum member, CHECK excludes them, the active-pair unique index still lists `LIVE_ACTIVE`.
- **Metadata:** `products`, append-only `product_metadata_snapshots`, mutable `product_metadata_current` as in the baseline; `allowed_transitions` seeded from the code table and compared by a test.
- **Metrics:** `tradingdots_pair_candidates_total` and `tradingdots_pair_state_total` published (gauges under contract names); `tradingdots_pair_metadata_age_seconds` is an extension. `tradingdots_bot_product_metadata_age_seconds` stays reserved (no bot).
- **VIEWERs** see transitions without actor names and no audit timeline.
- **FEE_MODEL_V1** (25th-percentile daily range / levels vs 2 x maker fee + edge, at attested and stress rates) is an engineering assumption, replaceable in Phase 5.
- **Files beyond an obvious pair list:** `app/config.py` (validation, fee and exchange settings), `app/api/app.py`/`dependencies.py`/`errors` untouched except wiring, `app/auth/session.py` (`reauth_is_fresh`), `app/monitoring/*` (pair metrics), `app/web/*`, `docker-compose.yml`, `Makefile`, `pyproject.toml` (httpx now a runtime dependency; version 0.4.0), existing tests updated for the new routes, roles, schema version and metrics.
- **Unverified:** real Coinbase response shapes (AS-C1, fixtures are synthetic), container start of `egress-proxy` and `pairs`, proxy against the live host.
- review: SELF (non-security deviations). **No independent security review has been performed.**

### DEC-014: Phase 4 verification record
- Full suite 1200 passed, 0 skipped (real PostgreSQL 16, real promtool/Prometheus/node_exporter); ruff, format and mypy strict clean.
- Real process + Chromium (pgserver DB, `app.cli serve`, runner against SYNTHETIC Coinbase data): 12/12 checks, including disable with password + exact phrase, wrong-case phrase refusal, no CSP violations, no horizontal scroll at 375 px.
- Not verified: any real Coinbase response (network denied, fixtures synthetic), Docker start of `egress-proxy`/`pairs`, proxy against the live host, Grafana.
- review: SELF

### DEC-015: Phase 5 market data, backtest and paper trading (2026-09-29)
Implemented: Decimal-safe money helpers, candle validation and data-quality events, public OHLCV importer (dry run by default), ingestion cursor, product metadata freshness gate, deterministic Parquet snapshots with checksums and provenance, indicators, trend/range filters, geometric grid builder, pair score, fee-aware backtest (operator and stress fees) with walk-forward, one shared pure trader used by both the backtest and the paper exchange, a local persisted restart-safe paper exchange, JSON and Markdown reports, read-only Reports pages, read-only metrics, host CLI. Deviations and choices:
- **New runtime dependency: pyarrow** (Parquet). Bytes are made deterministic (zstd, no dictionary, no stored schema); integrity comes from `file_sha256` and `content_sha256`, verified on every load.
- **No worker.** As in Phase 4, everything runs from the host CLI as `td_ctl` (`batch` service, profile `discovery`, same egress-proxy model as `pairs`). `td_app` is SELECT-only on all new tables.
- **PaperRuntimeGate replaces the always-refusing default gate.** A pair may activate only in PAPER mode while the paper session is PAUSED; production still refuses PAPER (`production_problems`). Open paper orders or inventory make a pair "not clean" (blocks disable/archive).
- **Capital backstop in the database:** hard-coded 50/15/35 constants, a single 50 USDC deposit, orders only for the PAPER_ACTIVE pair while the session is RUNNING. Independent of, and in addition to, the trader's own invariants.
- **Fees stay operator-attested** (`fees.*` in `config/pair-policy.yaml`); without a valid attestation the backtest and paper start refuse. Stress maker fee 0.006.
- **Not built (no live orders exist to act on):** flatten/dust write-off, kill switch, circuit breakers beyond the drawdown HALT, reconciliation. Growth and regridding stay disabled (refused in config validation and in the trader).
- **Metrics extensions** beyond the reserved catalogue: nine `tradingdots_ingest_*`, `data_quality_*`, `backtest_*`, `reports_*` and `paper_*` families (see `docs/backtest-and-paper.md`).
- **Strategy and simulation are engineering assumptions**, not validated edge. The FEE_MODEL_V1 of Phase 4 is unchanged.
- **Defects found and fixed while testing:** `--days 0` silently meant "default"; a too-short walk-forward raised a bare ValueError; a migration CHECK required a grid plan before one could exist; report detail overflowed at 375 px.
- review: SELF. **No independent security review has been performed.**

### DEC-016: Phase 5 verification record
- Full suite 1400 passed, 0 skipped (real PostgreSQL, real promtool/Prometheus/node_exporter); ruff, format and mypy clean.
- Real processes + Chromium (pgserver DB, real CLI subprocesses, `app.cli serve`, SYNTHETIC candles): 16/16 checks, including snapshot, backtest, idempotent repeat, paper refusal, web-role refusal, read-only files, plain-text attachment download, no horizontal scroll at 375 px, no CSP violations.
- Migration 0003 rollback to schema 2 verified on a real PostgreSQL (market and paper tables removed, pairs kept).
- Not verified: any real Coinbase response (network denied; all fixtures synthetic, AS-C1), Docker start of `batch`/`egress-proxy`/`pairs` (no Docker daemon), `/data` volume ownership in a container, `docker compose config`, Grafana.
- review: SELF

### DEC-017: Phase 6 read-only review packages (2026-09-29)
Implemented: disabled-by-default feature flag, ADMIN-only enable/disable/create with CSRF + fresh reauth + exact phrases, typed export views, sanitizer and scanner, deterministic ZIP builder with manifest/README/summary/JSONL/CSV/checksums/prompt, integrity verifier, protected POST download, retention cleanup, package dashboard, audit events for every action and refusal, aggregate metrics, host CLI (`review build|verify|cleanup|list`). No external LLM call, no proposal import, no bot mutation. Choices and deviations:
- **Numbering.** The request calls this Phase 6; the approved baseline calls it Phase 7 (baseline Phase 6 = reports/dashboard integration, delivered inside Phase 5). Scope followed the request.
- **Asynchronous creation, as the baseline's lifecycle (4.3) says.** The web tier records a REQUESTED row; the host builds it in `batch`. There is no worker, so `review build` is run by an operator (or a schedule), as with the other host commands.
- **Disable needs the full chain** (CSRF + fresh reauth + `DISABLE READ-ONLY REVIEW PACKAGES`), stricter than the baseline's CSRF-only F2, per the request.
- **Not built:** `DELETE REVIEW PACKAGE <ID>` (baseline P7): retention expires packages and keeps a tombstone; the request did not ask for delete. Audit-debt guard on enabling (no audit-debt concept exists yet). Risk-event, order-intent and reconciliation exports (those tables do not exist; the manifest lists them as not available).
- **New web-writable tables.** `td_app` may insert a REQUESTED row, change the feature flag (three columns) and mark a READY package CORRUPT; `td_ctl` performs all other transitions. A trigger enforces the state machine per actor class, immutability of request fields, single flight, 10 retained, 3 per hour, and "requests only while enabled".
- **Storage.** New `review_packages` volume: `batch` read-write, `app` read-only (`verify_security_config.py` checks it); files 0440, atomic, generated names not derivable from the package id; `TD_REVIEW_DIR` refuses web-root-like paths. The web tier now reads package files (download and verify) but imports no market/paper/exchange code.
- **Download** is a POST (baseline 4.3), verified on the exact bytes served; a failed check marks the package CORRUPT and serves nothing. Served as an opaque attachment with `nosniff`, `no-store` and a sandbox CSP.
- **Prompt.** The review prompt asks for a written advisory reply, not proposal JSON, because proposal import (baseline Phase 8) does not exist; the strict format belongs there.
- **Metrics extensions:** five `tradingdots_review_*` families, aggregate only.
- **Defects found and fixed while testing:** scope lists containing unknown or repeated items passed validation; a bit flip inside a deflate stream, and corrupted ZIP headers, raised uncaught exceptions during verification (found by fuzzing every byte); `README`/tests needed key-marker strings built by concatenation to satisfy the repository secret scan.
- review: SELF. **No independent security review has been performed.**

### DEC-018: Phase 6 verification record
- Full suite 1603 passed, 0 skipped (real PostgreSQL 16, real promtool/Prometheus/node_exporter); ruff, format and mypy clean.
- Real processes + Chromium (pgserver DB, real CLI subprocesses, `app.cli serve`, SYNTHETIC data): 25/25 checks: default DISABLED; wrong-case phrase refused without spending the reauth; enable, request, host build, verify, protected POST download; the downloaded ZIP verifies clean and holds no username, database password or path; GET download 405; no static route; VIEWER 403; a tampered file is refused and shown CORRUPT; disable with its own phrase; every action audited; no horizontal scroll at 375 px; no CSP violations.
- Byte-level fuzzing of a package (every byte flipped): verification never raises and never passes silently.
- Migration 0004 rollback to schema 3 verified on a real PostgreSQL (review tables removed; market and pairs tables kept).
- Not verified: Docker start of `batch`/`app` with the shared `review_packages` volume (no Docker daemon), `docker compose config`, Grafana, any real Coinbase data (AS-C1; all fixtures synthetic).
- review: SELF

### DEC-019: Phase 7 imported LLM proposals (2026-09-29)
Implemented: disabled-by-default import; ADMIN-only chain (CSRF + fresh single-use reauth + exact phrases); text/plain and application/json only; strict 15-field schema with review-package id and hash; policy triage and deterministic risk assessment; proposal lifecycle enforced by database triggers; manual change request; attestations; proposal UI with the label UNTRUSTED ADVISORY INPUT; host CLI (`proposal validate|cleanup|list`); aggregate metrics. No automatic application, no LLM call, no mutation of code, configuration, orders, pairs or system state. Choices and deviations:
- **Numbering.** The request calls this Phase 7; the baseline calls it Phase 8 (see DEC-017). Scope followed the request.
- **Web stores, host validates.** The web tier checks type, extension, bytes, UTF-8 and size and stores opaque bytes; it never parses the JSON. `td_ctl` (host CLI in `batch`) parses, checks the schema and package link, screens policy and assesses risk. Two consequences: a proposal is IMPORTED until an operator runs `make proposal-validate`, and a compromised web process cannot mark its own file VALIDATED (trigger-enforced actor classes).
- **File storage instead of a database blob** (baseline records proposals in the database, BQ-2): the file is kept for byte-exactness and the row keeps hash, size, parsed fields and findings. The database, not the file, is authoritative for state.
- **Disable, review and close are CSRF-only**, because each moves toward "less happens". Import, enable, change request and attestations need a fresh reauth.
- **Policy is over-inclusive.** Negated mentions are still blocked; the only exemption is a plain "no guarantee of profit" disclaimer. The policy is not a security boundary: the boundary is that no proposal action can change anything.
- **No delete.** Bytes of CLOSED/REJECTED proposals are purged after 90 days by `proposal cleanup`; the row and audit trail stay.
- **Limits:** 128 KiB per proposal, 10 imports per hour, 20 per day, 200 MB stored; upload route only gets a 400 KiB body cap and CSRF accepts exactly one file part there.
- **Attestations are human statements checked against evidence** (release reference; existing BACKTEST/WALK_FORWARD report ids created after the change request; at least two PAPER_DAILY reports spanning 7 days). They prove existence of evidence, not correctness of the change.
- **Metrics extension:** four `tradingdots_proposal*` / `tradingdots_llm_proposal*` families; aggregate only.
- **Defects found and fixed while testing:** policy gaps for passive/inflected verbs, standalone "auto apply", `api_key`-style nouns and short proximity windows; regex tables needed raw strings.
- review: SELF. **No independent security review has been performed.**

### DEC-020: Phase 7 verification record
- Full suite 2374 passed, 0 skipped (real PostgreSQL 16, real promtool/Prometheus/node_exporter); ruff, format and mypy clean. Note: the suite was run before the final one-line typing fix in `tests/unit/test_proposal_policy.py`; that file was re-run alone (212 passed).
- Real processes + Chromium (pgserver DB, real CLI subprocesses, `app.cli serve`, real multipart upload, SYNTHETIC data): 28/28 checks: default DISABLED and labelled; ZIP-named-.json and .html refused with nothing stored; valid upload stored as a generated 0440 name outside the web root, no static route; host validation; hostile markup rejected and shown only escaped; review; manual change request with a phrase naming the proposal id; before/after row counts of pair, order, paper, config and user tables unchanged; policy violation REJECTED and not reviewable; VIEWER 403; one-click disable; no horizontal scroll at 375 px; no CSP violations; no inline script; every action audited; no proposal text or secrets in the audit log.
- Migration 0005 rollback to schema 4 and re-apply verified on a real PostgreSQL.
- Not verified: Docker start of `app`/`batch` with the shared `proposals` volume (no Docker daemon), `docker compose config`, any real Coinbase data (AS-C1). Policy is heuristic; no independent review.
- review: SELF

### DEC-021: Phase 8 safety machinery (2026-09-29)
Implemented: pure risk engine, circuit breaker, kill switch, stale data/metadata and anomaly checks, GET-only private read adapter behind `NullSigner`, WebSocket hint parser, REST reconciliation, startup recovery, retry policy (reads only), ADMIN control workflow (four phrases), live gate that can only return BLOCKED, order path with immutable intents, scripted fake exchange and request-shape sandbox. Live remains blocked. Choices and deviations:
- **Numbering.** The request calls this Phase 8; the baseline calls it Phase 9 (safety machinery). Scope followed the request.
- **Adapter and WebSocket (deviation from CB-16 / paper-image rules).** The baseline keeps the private adapter and JWT signer out of the paper image and excludes WebSocket. The request asks for a private read adapter and user updates. Delivered: a **GET-only** adapter in `app/exchange` (not `app/adapters`), with a `NullSigner` as the only signer (no JWT/crypto code, no key loader), no write method, and a feed **parser** with no socket code. Every deployment has no reader and no gateway (`factory.py` returns None). The public client is unchanged and still the only egress user; the proxy allowlist is unchanged.
- **No real gateway.** Kill switch "cancel known orders" and order submission run through an `ExecutionGateway` protocol whose only implementation is the test double. Without a gateway a cancel command FAILs loudly (`GATEWAY_UNAVAILABLE`); the paper venue is cancelled through the paper exchange.
- **Bot control is the state authority for the new order path; the paper trader only honours kill and breaker** (start refused, next step cancels and pauses). Making paper depend on RUNNING/reconciliation would need a paper reconciler and is deferred to the paper deployment review.
- **RESUME is web-only and needs a host-produced reconciliation** (the web tier has no egress). The database enforces it (fresh OK run <= 300 s, finished after any breaker trip, no UNKNOWN attempt). Recovery, breaker opening and kill release are host-only. Kill release leaves the bot PAUSED and recovery INCOMPLETE.
- **All four controls use the full chain** (CSRF, fresh reauth, typed phrase), stricter than the baseline's CSRF-only restrictive actions, per the request. Audit is written before (`bot.control_requested`) and after (outcome or denial) the internal command.
- **Times in guards are the application clock** carried in the row, not `clock_timestamp()` (BI-36 deviation, as in earlier phases); the initial control row is dated `epoch`.
- **Absence proof is conservative and unverified**: two OK reconciliations started at least 120 s after the submit mark, none naming the client id. A later appearance is a blocking finding and trips the breaker. The baseline defers a real absence proof to Phase 11.
- **Ceilings in the schema:** per-order notional <= 12 USDC (new constant), post-only limit GTC only, venues PAPER/FAKE only. An over-cap intent is refused by the schema and reported as `INTENT_REFUSED`.
- **Equity for loss/drawdown** is sampled at recorded fills (price of each fill as the mark); with no fills equity equals the baseline. It is deterministic and needs no price history.
- **Metrics:** thirteen `tradingdots_bot_*` families are now published from real tables (reserved-list wording and tests updated deliberately); labels avoid the forbidden `error`/`order` words (`failure_class`).
- **Test double provenance:** FAKE-EXCHANGE never matches orders or fills anything by itself; sandbox code validates shapes only.
- **Defects found while testing:** an over-cap intent raised instead of being reported; the reconciler did not flag an order wearing the id of a rejected attempt; absence proof could not skip an id a run had named (the database refused the whole run); list results with equal timestamps needed an insertion sequence to order runs.
- review: SELF. **No independent security review has been performed.**

### DEC-022: Phase 8 verification record
- Full suite (real PostgreSQL 16, real promtool/Prometheus/node_exporter): 2973 passed and 1 failed on the first run; the failure was a stale assertion in `test_prometheus_scrape.py` (it expected the now-published kill-switch and open-order series to be absent). It was corrected and that file re-run: 48 passed. A final full run gave 2973 passed and 1 failed: a Phase 6 test that stamped a report with the real database clock against a fixed fake-clock period broke when the real date rolled over (it fails identically at the Phase 7 commit); it now uses the fake clock and the review test files pass (105). ruff, format and mypy clean.
- Real processes + Chromium (pgserver DB, real `app.cli serve`, real CLI subprocesses, SYNTHETIC data): 28/28 checks: Bot page state and LIVE TRADING BLOCKED, no order form, RESUME refused with reasons, wrong-case phrase refused without spending the reauth, kill switch activation queues one cancel and changes nothing else, host `recover`/`reconcile` refuse with `NO_EXCHANGE_READER`, cancel command runs, kill release needs its phrase and leaves the bot PAUSED with recovery INCOMPLETE, viewer read-only, no horizontal scroll at 375 px (a real overflow was found and fixed), no CSP violations, every action audited, no password or phrase in the audit log, no order ever created.
- Fault injection with the scripted FAKE exchange: see the fault matrix in `docs/safety-machinery.md`.
- Migration 0006 rollback to schema 5 and re-apply verified on a real PostgreSQL.
- Not verified: any real Coinbase response (AS-C1, AS-C3, AS-C4), client-id scope, the absence proof against a real exchange, WebSocket connectivity (no client exists), Docker start of any service (no Docker daemon), a paper reconciler. Live trading remains BLOCKED; no independent review.
- review: SELF

### DEC-023: Phase 8 security-review fixes
Self-review (security-engineer pass) found one BLOCKER, three HIGH, two MEDIUM issues; all six are fixed in place in migration 0006 (unreleased, so edited rather than superseded; `0006_safety.down.sql` updated to match).
- B1 web could jam the control clock by writing a future `updated_at`; H1 web could back-date to fake a fresh reconciliation: the database clock `td_now()` is now the only trusted clock. A supplied time is accepted only within 60 s of it (`td_check_time`, on control, decisions, attempts, reconciliation runs). Tests move the clock through an owner-only `td_test_clock` table, empty in every deployment and unreachable by `td_app`/`td_ctl`.
- H2 restart safety was not DB-enforced: recovery now binds a `boot_id`; `order_attempts` refuses any attempt whose boot id is not the recovered one.
- H3 reserve/cap/loss/drawdown were Python-only: `td_authorize_order` recomputes them in SQL at the hard ceilings (conservatively) when an attempt is inserted. Python config may only tighten them.
- M1 freshness and absence were venue-blind: both are per venue. M2 absence proof now needs two OK runs at least 60 s apart after the 120 s window.
- Still OPEN (not fixed): M3 caller-supplied order book is not independently verified; M4 no scheduler/heartbeat (every host action is operator-driven); M5 no retention policy for safety tables; M6 restrictive web actions depend on audit availability; M7 reconciliation lookback is limited. Lows: VIEWER sees client order ids; FAKE venue legal in the production schema; no per-session phrase throttle; backup docs; the 0006 down-migration destroys safety history. Loss/drawdown SQL limits are not covered by a dedicated regression test.
- review: SELF. **Still no independent security review.**

### DEC-024: Capital profiles, exchange-funds check and funds-based grid sizing
- Owner request: size from the actual available USDC, a selectable profile for paper and live, use the grid sizing. The fixed ceilings 50/15/35/12 (constants and SQL) become four immutable profiles (`pilot` equals the old values and is the default); migration 0007 (new, 0006 is not edited again).
- **Baseline deviation (logged):** the hard ceilings are now per profile. The largest profile allows a 250 cap, 50 reserve, 150 deployment and 50 per order. The database enforces the selected profile in `td_authorize_order` and the paper capital/deposit triggers, on top of absolute table CHECKs (50 per intent, 150 per grid cell). Only `pilot` was ever validated by the earlier phases' real-browser and soak evidence; `expanded` and `medium` are exercised by tests only.
- Selecting a profile is ADMIN-only, bot PAUSED, never combined with another control change (DB-enforced), phrases as specified, fully audited (`bot.profile_changed`). LIVE selection is recorded only; LIVE remains unrepresentable and the gate stays blocked.
- BUY orders must fit `usable_quote` computed from the funds the read adapter reports (`FUNDS_UNAVAILABLE` / `INSUFFICIENT_FUNDS`); the paper trader sizes from its ledger. Fewer grid lines are tried before NO_TRADE.
- Not verified: any real Coinbase balance response (no signer or credentials exist); the loss/drawdown SQL ceilings are not scaled by profile; `expanded`/`medium` have no paper soak.
- review: SELF. **No independent security review has been performed on this change.** Because it raises the maximum capital the code can be configured to use, it needs one before any profile other than `pilot` is used outside tests.

### DEC-025: Phase 9 security-review fixes
Fixes for the Phase 9 security review (H1, H2, H3, M2, M4, M5, L1). New migration 0008 (0006 and 0007 are not edited).
- H1 (reproduced before the fix): the paper profile could change while the paper session ran, and a downgrade with inventory made the safety cancel fail. The database guard now also requires `paper_session` PAUSED, no OPEN paper orders, and existing cash/inventory inside the new profile's limits; the paper capital check never blocks a cancel that only releases reserve. Regression tests include the exact reproduced state (forced with the guard disabled), where the cancel now succeeds.
- H2: `order_attempt_guard` takes `bot_control` FOR SHARE and runs the money rules under `pg_advisory_xact_lock`, so a kill switch, pause or profile change waits for an authorization in flight and two authorizations cannot both pass the reserve. Negative control: with the two locks removed, both new concurrency tests fail.
- H3: `migrate` records a SHA-256 per migration in an owner-only `schema_migrations` table and refuses to run if an applied file changed. Limit: a database that predates this check is trusted once (its checksums are recorded on the first run), so an earlier in-place edit of 0006 is still undetectable there.
- M2: a failed funds read is recorded as an `api_events` row (`FUNDS_READ_FAILED`) and counts toward `API_FAILURES`. M4/M5: production refuses any capital profile but `pilot` (config validation and the control service). L1: the audit records the live-gate status from the gate itself.
- Still open (unchanged): M1 the exchange-funds check is host-process only (the database cannot read the exchange); M3 the paper deposit is fixed at the first start; the carried-over mediums/lows (no scheduler, retention, bot alerts, database-outage test, backup/restore test, independent review).
- review: SELF. **No independent security review has been performed.**

### DEC-026: Live trading mode, editable trading configuration, parallel pairs (owner-directed, security to be restored later)
The owner asked for live trading with automated position making, a data feed, a configurable number of pairs / grid lines / USDC per grid / invested and reserve capital, and a paper-to-live switch, and said security will be implemented again later. The fund protections were kept on request: kill switch, protected reserve, caps, fund checks, reconciliation, post-only orders, no market sell.
- **What exists now:** venue `COINBASE` (migration 0009); CDP ES256 credentials from one 0600 file outside the repository and a signer (`app/exchange/credentials.py`, `cdp_signer.py`); a create/cancel-only gateway (`coinbase_live.py`: ambiguity means UNKNOWN, reconcile before any retry); per-mode `trading_config` and `trading_state` (migrations 0010, 0011: mode BACKTEST/PAPER/LIVE); `live_grids` (0012); the grid runner (`app/live/runner.py`) and `live` CLI (`status, check, baseline, arm, disarm, config, run`); the Bot page shows and edits both (mode buttons, trading configuration form, each with ADMIN, CSRF, fresh reauth, typed phrase, audit `bot.mode_switched` / `bot.config_changed`).
- **Protections kept (database-enforced):** orders only through the pipeline (intent, risk decision, `td_authorize_order`); the reserve is never invested (and must be at least 20% of invested plus reserve); the invested cap and the per-order cap (applies to SELLs too) are checked in SQL from the LIVE configuration; funds are read from the exchange before every BUY; the kill switch cancels known orders and blocks everything; the breaker, reconciliation freshness, startup recovery with a boot id, UNKNOWN attempts, spread, stale data, loss and drawdown limits still block. Live orders need the host arming (<= 24 h, revoke-only); an arming also ends on a kill switch, breaker trip, recovery reset, LIVE profile change, LIVE configuration change or any mode change made after it. Arming refuses a key that can transfer funds. Switching the mode never places an order and never arms.
- **Security relaxations made on the owner's instruction (TO RE-ENABLE LATER):** (1) LIVE limits are no longer fixed at the pilot profile; they are whatever the LIVE configuration says (defaults still 35 invested / 15 reserve / 12 per order, one pair, 3 lines); (2) up to 10 active pairs (was 1) and 3 to 20 grid lines (was 3 to 5); the schema per-intent ceiling was 12 and is now 1,000,000 (the real limit is the configuration); (3) the production-only-`pilot` rule does not apply to configuration edits; (4) the five human attestations (key permissions, fee tier, etc.) are no longer required to arm; (5) the paper side still trades one pair only; (6) the loss ($10) and drawdown (20%) ceilings are not scaled to larger configurations.
- **Known design limits:** grid progress is derived from intents and attempts (no separate ledger); a SELL is placed for what a cell holds, just above the market if the market already passed its line; there is no WebSocket feed (REST candles and a REST book every tick); response shapes of the live API are unverified (AS-C3, AS-C4) and the first VPS run is the network test; `live run` recovers on every start, which pauses the bot (resume on the Bot page, then `live arm`).
- Not verified: any real Coinbase request or response, signing against the real API, a real fill, Docker start of the `live` service, Caddy/TLS.
- review: SELF. **No independent security review has been performed, and this change removes protections that must be restored before real money beyond a small test.**
### DEC-027: Caddy behind the host's Apache on loopback 8080/444
The VPS already runs Apache on 80/443 for another site, and the operator chose to keep it (2026-10-01).
- Compose publishes Caddy on `127.0.0.1:8080` and `127.0.0.1:444` only; `edge_public` has the static subnet `172.29.30.0/24`. The verifier requires exactly these two ports and the loopback address (Docker bypasses ufw for published ports).
- Apache (`infra/apache/tradingdots.conf`) terminates public TLS with its own certbot certificate, removes client-sent `X-Forwarded-*`/`Forwarded`, and proxies to Caddy over TLS, verifying Caddy's own certificate. Caddy gets that certificate over HTTP-01 through Apache's port 80 (TLS-ALPN disabled).
- Caddy trusts `X-Forwarded-For` only from `172.29.30.1` (strict, right to left) and sends the app a single address. Without this the app's rightmost-entry rule would see the Docker gateway for every visitor and login throttling would be global (threat T22).
- Verified on the VPS with Caddy alone (app not started): `caddy validate`; both certificates issued; blocked paths answer 404 through Apache; a request with forged `X-Forwarded-For: 6.6.6.6` and `X-Forwarded-Proto: http` reached Caddy with the real address and `https`; 8080/444 listen on 127.0.0.1 only. Not verified: the address the app itself records, Grafana through the chain, an external port scan.
- New exposure: Apache is now part of the trusted path (it sees plaintext requests, cookies included, and its access log keeps full client addresses); its `Server` header replaces the one Caddy removes.
- review: SELF. **No independent security review has been performed.**

### DEC-028: The Coinbase key variables are accepted settings; first real Coinbase response
Found on the VPS on 2026-10-01 after the first key install.
- `load_settings` rejected `TD_COINBASE_KEY_FILE` as unknown. Compose always sets it for `batch` and `live`, so every command of those services failed, and after `06-install-key.sh` wrote it (with `TD_COINBASE_KEY_HOST_PATH`) to `.env` the environment-file check failed too. Both names are now accepted control variables; neither maps onto `Settings`, and the key is still read only by `app.exchange.credentials`. A refused key file met outside `live` is reported by its fixed code instead of a traceback.
- Verified on the VPS with the real key: signing and `GET /key_permissions` work through the egress proxy (view and trade yes, transfer no, portfolio DEFAULT); a bare UUID key id is accepted as the key name. This is the only real Coinbase response this code has parsed; no other private endpoint has been called and no order has been placed.
- review: SELF. **No independent security review has been performed.**

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
