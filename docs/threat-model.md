# Threat model (Phase 1)

Scope: the Phase 1 shell only. No trading, credentials, or business data exist yet, which bounds the impact.

| # | Threat | Mitigation in Phase 1 | Status |
|---|---|---|---|
| T1 | Accidental or malicious enabling of LIVE | `LIVE` not representable (mode allowlist, `TD_PROFILE` allowlist, unknown `TD_*` rejected, `live.example.yaml` never loaded); banner fixed to BLOCKED; no exchange code exists | Tested (`test_config.py`) |
| T2 | Internal service exposed publicly | Only Caddy publishes 80/443; internal networks; validator + tests fail on any other port, host network, docker.sock, privileged | Tested statically; **not** verified on a running host |
| T3 | Weak production config (debug, placeholder secrets, insecure cookies, bad hostnames) | `load_settings` refuses production start; `make verify-security-config` checks `.env` (incl. mode 600) | Tested |
| T4 | Secret leakage via logs/errors/health | Redacting formatter; generic error pages; `/healthz` fixed body; `.env` git-ignored; scans for key material | Tested |
| T5 | XSS / clickjacking / content sniffing | Autoescape, no inline scripts, CSP `default-src 'none'` + self, `frame-ancestors 'none'`, nosniff, htmx `allowEval=false`, vendored checksum-pinned HTMX | Tested |
| T6 | Host header abuse / unknown vhost | TrustedHost middleware; Caddy serves configured hosts only | Tested (app); Caddy unverified |
| T7 | Path traversal via static | Starlette StaticFiles; tests for encoded traversal | Tested |
| T8 | Introspection (`/docs`, `/openapi.json`, `/metrics`) | Disabled in app; 404 at Caddy | Tested (app) |
| T9 | Unauthenticated access to dashboard | Resolved in Phase 2: every page except `/login` and `/healthz` requires a session (default-deny guard, route-inventory test) | Tested |
| T10 | Compromised container | Non-root app user, `cap_drop: ALL`, `no-new-privileges`, read-only root fs, tmpfs, memory/pids limits, no egress | Config only; not runtime-verified (no Docker daemon available) |
| T11 | Supply chain (images, packages) | `uv.lock` pins Python packages; HTMX integrity-checked against the npm registry hash | **Open**: base image digests not pinned; lock has no hash-verified install in the image beyond uv defaults |
| T12 | Secret exposure through Compose environment | Secrets come from `.env` (0600) into container env (visible via `docker inspect` to Docker admins) | **Accepted for Phase 1**; file-based secrets are a later hardening |
| T13 | Single host, no independent watcher or off-host backup | Owner decision DEC-006: not implemented or planned. Risk accepted by the owner | Accepted; keeps live blocked |

Not modelled yet: authentication, CSRF, sessions, audit tampering, exchange failure modes, LLM content, uploads.

## Phase 2 additions
| # | Threat | Mitigation | Status |
|---|---|---|---|
| T14 | Credential guessing / stuffing | Argon2id, generic failure, account+client, client-wide and account-wide throttles, throttled reauth/password-change | Tested |
| T15 | Account enumeration | Identical response and equivalent work for unknown/disabled/wrong; throttle keys use the submitted name whether or not it exists | Tested |
| T16 | Session theft/fixation/replay | Opaque hashed tokens, `__Host-` HttpOnly Secure Strict cookies, rotation on login/password change, idle+absolute expiry, revocation, `Clear-Site-Data` | Tested (client and Chromium) |
| T17 | CSRF / login CSRF | Session-bound HMAC tokens, separate pre-session login token, Origin/Fetch-Metadata, SameSite=Strict, app-wide guard | Tested (incl. real cross-site form) |
| T18 | Privilege escalation / IDOR | Permission matrix, default deny, object checks on session ids, extra form fields rejected, service-level permission check | Tested |
| T19 | Audit tampering | Append-only triggers (also for the owner), hash chain verified on `/audit`, web role cannot modify history | Tested; **no off-host anchor (DEC-006)** |
| T20 | Web-process compromise creating users | Only `td_ctl` can INSERT users; but a compromised web process can still act as signed-in users and change passwords (RR-1) | Partly mitigated; live blocker |
| T21 | Lock-out of the ADMIN by attackers | Account-wide throttle can be triggered by distributed guessing; the window is 15 min and the host CLI can always rotate the password | Accepted |
| T22 | Wrong client identity behind the proxy | `X-Forwarded-For` honoured only from `trusted_proxies`; mismatch with the Compose subnet would make throttling global | Runbook warning; unverified on a host |

## Phase 3 additions
| # | Threat | Mitigation | Status |
|---|---|---|---|
| T23 | Metrics exposed publicly | Separate internal listener bound to a private address, allowed scrapers only, never proxied; Caddy 404 for `/metrics*`; no host ports for any monitoring service | Tested; Caddy/host unverified |
| T24 | Sensitive data or identities in metrics/dashboards | Fixed label sets, forbidden label names, sanitiser, no free text; dashboards checked for the same | Tested (incl. hostile input) |
| T25 | Cardinality explosion by an attacker | Route templates only, fixed value sets, 100-series cap per family | Tested |
| T26 | Monitoring used as a control plane | No Alertmanager/webhook/transport, Grafana alerting off, Prometheus lifecycle/admin APIs disabled, rules restricted to labels/annotations without actions or URLs, read-only provisioned dashboards, web tier never queries Prometheus | Tested (real Prometheus) |
| T27 | Grafana takeover | Unique admin user and required strong secrets (no defaults), login protection, no sign-up/anonymous/embedding/plugins, secure strict cookies, isolated from the app network | Config tested; Grafana not run here |
| T28 | Compromised monitoring container reaching the host | Non-root, read-only rootfs, `cap_drop: ALL`, read-only host mounts, no Docker socket or Docker data (cAdvisor included), internal networks only | Config tested; runtime unverified |
| T29 | Monitoring outage affecting the application | Listener failure is non-fatal; snapshot and summary never raise; database outage becomes `db_up 0` | Tested |
| T30 | Misleading "all clear" from missing data | Absent series instead of zeros; TargetDown/absent alerts; dashboards state what is not measured | Tested |
| T31 | No alert delivery (by design) | Alerts are only visible if someone looks: daily check in the runbook; residual risk accepted | Accepted |

| T32 | Pair injection (a crafted product or ID becomes a pair) | Only a discovered product UUID can be proposed; product ids match a fixed pattern; alias/status reduced to a safe vocabulary; duplicates and the 20-pair cap enforced under an advisory lock | Tested |
| T33 | Activation or archive by a stolen session or forged request | ADMIN + CSRF/Origin + fresh single-use password + exact typed phrase + version check; each denial audited | Tested |
| T34 | Two active pairs (race) | Unique index over the active states, advisory lock, service guard; 5-way concurrent test yields one active pair | Tested |
| T35 | Stale or changed metadata treated as valid | PASS run must be the current evidence, unexpired, on the current snapshot, metadata within the age limit | Tested |
| T36 | Exchange content as XSS | Parse-time reduction plus autoescape; hostile stored content tested on every pair page | Tested |
| T37 | SSRF / open proxy via the runner | Fixed client host and paths, no redirects, proxy allows one CONNECT target and rejects private resolutions | Tested; proxy not run in a container here |
| T38 | Wrongly optimistic fee model | Fees must be operator-attested and re-attested; model version recorded; both attested and stress rates required | Assumption documented (FEE_MODEL_V1) |

## Phase 5 additions
| Threat | Control | Residual |
|---|---|---|
| Poisoned or malformed candle data | Validation excludes and records; conflicts never overwrite; nothing invented | A consistently wrong upstream cannot be detected |
| Tampered snapshot file | Two checksums verified on load; files read-only; manifests | Someone with DB write access could change both row and file |
| Paper results mistaken for real | BACKTEST/PAPER labels, DB label CHECK, banner, limitations text | Human interpretation |
| Overfitting via parameter search | Walk-forward with out-of-sample reporting; tiny grid | Backtests remain optimistic in ways not modelled |
| Runaway paper capital | Trader invariants plus DB trigger constants | None known |
| Fee assumptions wrong | Attested fees required; stress scenario at 0.6% | Attestation is a human claim |

## Phase 6 additions (review packages)
| Threat | Control | Residual |
|---|---|---|
| Secret or private data leaks into a package | Typed rows (no free text), allow-listed SQL, aggregated audit data, scanner on the bytes at build and at every verify | A future column added to an export schema without review |
| Prompt injection through package content | Contents are typed numbers, codes and ids; the prompt template labels everything untrusted data and forbids commands; the package cannot act | A person pasting a package into a tool that can act |
| Tampered or swapped package file | Row cross-check (SHA-256, manifest hash, period, scope), per-file checksums, allowed paths, read-only files, verified-before-serve on the same bytes | Whoever can rewrite both the database row and the file |
| Path traversal, symlink or zip bomb in a package | Path allowlist, symlink/encryption refusal, size and expansion limits, no extraction to disk | None known |
| Download used to read arbitrary files | Storage names validated (`uuid.zip`), opened with no-follow, no path input from the user | None known |
| Unauthorised or CSRF-driven enable/download | ADMIN permission, CSRF and Origin check on POST, single-use reauth and exact phrases, audit of every refusal | A compromised ADMIN session that also knows the password |
| Package feature used to change bot state | Review tables reference only `users`; web role cannot build; a test hashes all bot/pair/paper tables around every action | None known |
| Package build starves the host | One package at a time, 10 retained, 3 per hour, size and row caps, retention cleanup | None known |

## Phase 7 additions (imported proposals)
| Threat | Control | Residual |
|---|---|---|
| Malicious file (archive, polyglot, binary, oversize) | Type, extension, magic-byte, UTF-8, JSON-object and size checks before storage; opaque stored bytes; generated name; never extracted or executed | A parser bug in the standard library JSON module |
| Prompt injection inside a proposal | The text is data: never executed, never sent to an LLM, policy-screened, labelled UNTRUSTED ADVISORY INPUT | A person who blindly follows a proposal |
| XSS / template injection through proposal text | Autoescape, no `|safe`, no `from_string`, markup and template syntax rejected by policy, no raw view | A future template that disables escaping (static test guards) |
| Proposal that asks to weaken risk, secrets, security, pairs or live mode | Policy rules reject it; more importantly no proposal action can change any of those | Heuristic false negatives (caught at human review and at the normal code review) |
| Automatic application | There is no apply code path; approval writes one change-request row; test hashes bot/pair/paper/config tables around every action | None known |
| Forged or replayed approval | ADMIN + CSRF + Origin + single-use reauth + phrase naming the proposal id | Compromised ADMIN session that knows the password |
| Upload flooding | Body caps, per-hour/day limits, 200 MB store cap, early refusal without a session | None known |
| Tampered stored file | Web role cannot validate; state and hashes recorded; content immutable in the row | Someone with DB and volume write access |
