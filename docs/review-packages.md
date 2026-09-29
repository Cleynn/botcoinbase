# Read-only review packages (Phase 6)

A review package is a **sanitized, historical, checksummed ZIP** that an ADMIN may give to an AI
assistant **by hand**. The trading system never calls an LLM and never reads anything back from one.
A package **cannot trade** and **cannot change** any bot, pair, strategy, risk, order, ledger,
configuration or live setting. Live trading stays BLOCKED (DEC-000 is not acknowledged; no
independent security review has happened). No proposal import exists in this phase.

> Numbering note: the approved baseline calls this "Phase 7" (its Phase 6 is reports and dashboard
> integration, which was delivered inside Phase 5). This document follows the request's numbering.

## Lifecycle

```
DISABLED (default) --[enable]--> ENABLED --[disable]--> DISABLED
ENABLED: request -> REQUESTED -> GENERATING -> READY | FAILED
                              READY -> CORRUPT (verify/download failed) | EXPIRED (retention)
                              CORRUPT, FAILED -> EXPIRED
```

| Action | Who | Chain | Audit event |
|---|---|---|---|
| Enable | ADMIN (web) | CSRF + fresh single-use password reauth + `ENABLE READ-ONLY REVIEW PACKAGES` + retention days (1-90) | `review.enabled` |
| Disable | ADMIN (web) | CSRF + fresh reauth + `DISABLE READ-ONLY REVIEW PACKAGES` | `review.disabled` |
| Request a package | ADMIN (web) | CSRF + fresh reauth + `CREATE READ-ONLY REVIEW PACKAGE` + period + scope | `review.requested` |
| Build | host CLI (`td_ctl`) | `review build` | `review.generating`, `review.ready` / `review.failed` |
| Verify | ADMIN (POST) or host CLI | CSRF (web) | `review.verified` / `review.corrupt` |
| Download | ADMIN (POST) | CSRF + Origin check; verified first | `review.downloaded` |
| Retention cleanup | host CLI | `review cleanup` | `review.expired`, `review.cleanup` |
| Any refusal | | | `review.denied` (reason code, no input echoed) |

A wrong phrase never consumes the reauthentication. Disable is stricter than the baseline (which
asked for CSRF only): it needs the full chain. Deleting a package by hand (`DELETE REVIEW PACKAGE`)
is not built; retention removes content and keeps a tombstone row.

Limits (enforced in the service **and** by database triggers): at most 10 retained packages
(REQUESTED, GENERATING, READY, CORRUPT), one waiting or building at a time, 3 requests per hour,
period at most 90 days and not in the future, at most 20 MiB per package (hard ceiling 50 MiB).

## What is in a package

| File | Content |
|---|---|
| `manifest.json` | schema `tradingdots.review-package/1`, package id, exporter version, period, scope, `advisory_only: true`, `can_trade: false`, configuration and strategy SHA-256 (hashes only), report ids, snapshot ids with file/content SHA-256, file list with sizes/SHA-256/row counts, not-available list, limitations, warning |
| `README.md`, `prompt/llm_review_prompt.md` | fixed text: what it is and is not; a safe review prompt that says the contents are untrusted data and the reply is advisory |
| `efficiency_summary.json`, `csv/summary.csv`, `csv/timeline.csv` | figures computed from the exported rows only |
| `data/*.jsonl` | typed rows by scope: `backtests` (runs, reports index, snapshots), `paper` (orders, fills, summary), `pairs` (lifecycle), `data_quality` (runs, event counts), `grid_plans`, `audit_summary` (counts per day) |
| `CHECKSUMS.sha256` | SHA-256 of every other file |

Not included because it does not exist yet: risk events, order intents, reconciliation summaries
(listed under `not_available`). The ZIP is deterministic: fixed timestamps, sorted entries, no
symlinks; the same inputs give the same bytes.

## What is never in a package

Secrets, keys, tokens, passwords and hashes, session ids, cookies, authorization headers, raw
private exchange payloads, database URLs, source IPs, user agents, filesystem paths, exception
traces, personal data (usernames, user ids), execution-capable content, raw proposal content,
display strings and **every free-text column**. Two independent layers enforce it:

1. **Typed export views** (`app/review/exporter.py`): each row is rebuilt from a schema of typed
   fields (fixed vocabulary, bounded numbers, canonical decimals, UUIDs, product ids, timestamps,
   SHA-256). A value of any other shape is refused, not cleaned; unknown keys are refused; the SQL
   selects allow-listed columns and aggregates audit data to counts (no actor, target, reason,
   client tag, request id or detail ever leaves the database). A `reason_code` that is not a fixed
   code is dropped to null.
2. **Scanner** (`app/review/sanitizer.py`) over the finished bytes: private-key markers, URLs,
   e-mail addresses, IPv4/IPv6, filesystem paths, exception traces, JWT-like and long token-like
   runs, action-capable text (scripts, `curl`, `sudo`, order/API paths) and, for data files,
   secret-like words. Any hit fails the whole package and purges the output. The same scan runs
   again on every verification, so a file rewritten together with its checksums is still caught.

CSV cells that could run as spreadsheet formulas (`= + - @`, tab, CR) are prefixed with `'`; plain
numbers, including negatives, are left alone.

## Storage and access

Files live in `TD_REVIEW_DIR` (default `/review`, the `review_packages` volume): **outside the web
root** (configuration validation refuses paths containing `static`, `web`, `public` or
`templates`, or under `/app`), under a **server-generated name that is not derivable from the
package id**, written atomically, `fsync`ed and read-only (0440). `batch` mounts the volume
read-write; `app` mounts it **read-only**; no other service mounts it (`verify_security_config.py`
checks this). There is no static route and no GET download: the only way to fetch a package is
`POST /review/packages/{id}/download` as an ADMIN with CSRF and a same-origin request. The service
reads the file **once**, verifies exactly those bytes (ZIP structure, allowed paths, no symlinks or
encryption, size and zip-bomb limits, strict manifest schema, every checksum, database row
cross-check, row schemas, scan) and serves only verified bytes as `application/octet-stream` with
`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and
a `sandbox` CSP. A failed check marks the package CORRUPT, audits `review.corrupt` and serves
nothing. Package pages show only metadata (state, period, file names, sizes, digest prefixes);
package content is never rendered. Checksums show **integrity, not authenticity**.

## Creation changes nothing else

The tables reference `users` only. The web tier can enable/disable the feature and insert a
REQUESTED row (and mark a READY package CORRUPT); the host can move a package through its states;
neither can touch any bot, pair, risk, order, ledger, configuration or gate table through this
feature. A test hashes every such table before and after enable, request, build, verify, download,
cleanup and disable and requires identical results.

## Commands

```
docker compose --profile discovery run --rm batch review build          # build requested packages
docker compose --profile discovery run --rm batch review verify --all   # or --id <uuid>
docker compose --profile discovery run --rm batch review cleanup        # retention + orphans
docker compose --profile discovery run --rm batch review list
make review-build | review-verify | review-cleanup | review-list
```

Enable, disable, request and download are web actions (Review packages page, ADMIN only).

## Metrics (aggregated only)

`tradingdots_review_enabled`, `tradingdots_review_packages{state}`,
`tradingdots_review_last_ready_timestamp_seconds`, `tradingdots_review_downloads_total`,
`tradingdots_review_denied_total`. No package id, name, path or content is ever a label or value.

## Rollback

Disable the feature (ADMIN, full chain). To remove the schema (development and test only):
`rollback(target, to_version=3)` applies `0004_review.down.sql` (drops the two review tables; audit
events stay because the audit log is append-only). Package files on disk are not removed by it: run
`review cleanup` first, or delete the volume contents by hand.

## Known limits

Creation is asynchronous: the web action records a request and the host builds it. Checksums prove
integrity only. The review prompt asks for a written advisory reply; a strict machine-readable
proposal format belongs to the (unbuilt) proposal import phase. No package delete action, no
per-package encryption, no signature. The `batch` container and the shared volume were not started
(no Docker daemon).
