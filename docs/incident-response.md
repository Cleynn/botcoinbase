# Incident response

Status: **SEED (Phase 1.0)** from baseline section 5.8, amended by DEC-006 (no second host). Paper phases risk no client funds; data, secrets and host integrity still matter.

Severity: **SEV1** suspected compromise, audit/anchor mismatch, credential found anywhere. **SEV2** duplicate/unknown/mismatched orders, reconciliation failure, ledger breach, data-loss risk. **SEV3** degraded monitoring, stale data, disk warnings.
Common steps: detect, contain (kill switch first), preserve redacted evidence, eradicate, recover through the normal recovery path, review, record in `decisions.md` if a baseline change follows.

| Trigger | First actions |
|---|---|
| IR-1 Suspected compromise | Kill switch (web or host); revoke all sessions (`ctl`); rotate DB role passwords, HMAC and signing keys, Grafana password and secret key, second-layer credential; review the audit log for gaps (no independent anchor exists, DEC-006); snapshot; restore clean if integrity uncertain. |
| IR-2 Audit or anchor mismatch | Treat as IR-1; do not delete rows. No second host exists (DEC-006), so integrity cannot be independently proven. |
| IR-3 Credential found anywhere | SEV1; revoke at Coinbase; remove; record as baseline violation. |
| IR-4 Duplicate/unknown/mismatched/fill-anomalous orders | Bot is BLOCKED by design; no resubmission; reconcile through the service; abandon only via the paper-only flow. |
| IR-5 LEDGER_BREACH / LEDGER_INCONSISTENT | Bot BLOCKED; do not edit the ledger; use documented write-off, flatten or rebuild-and-compare. |
| IR-6 Config hash mismatch | Bot BLOCKED; activate the intended registered config (bot PAUSED) or redeploy the registered image. |
| IR-7 Data or metadata anomaly | NO_TRADE stays; inspect candle conflicts, drift, clock offset; re-run public smoke test; re-verify Coinbase pages. |
| IR-8 Disk full | NO_TRADE; free logs and old staging backups; never delete business rows. |
| IR-9 Monitoring outage | Status line shows UNKNOWN; trading unaffected; restart monitoring. |
| IR-10 Malicious proposal/package content | Disable import; confirm trading tables unchanged; keep content inert. |
| IR-11 Upgrade failure | Stop; follow roll-forward rules. |
| IR-12 Host loss | No off-host backup exists (DEC-006); local data is lost with the host. Rebuild from the repository. |
