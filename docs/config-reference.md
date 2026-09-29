# Configuration reference

Status: **SKELETON** (Phase 1.0). Not yet binding: DEC-000 is unacknowledged.

Source of truth: `baseline/TRADINGDOTS_HANDOFF.md`, Section 4.9 (ownership) and fixed defaults.

To be written in the phase that introduces the behavior. Do not paste unverified claims here.

## Phase 5 additions
- `config/pair-policy.yaml` `strategy:` and `backtest:` blocks (bounded, Decimal strings; see `docs/backtest-and-paper.md`).
- `TD_DATA_DIR` (`data.data_dir`, default `/data`): snapshot files live in `<data_dir>/snapshots`. `data.history_days` (7-365, default 90), `data.max_attempts` (1-5, default 3), `data.backoff_seconds`.
- The `batch` service needs `TD_DB_CTL_PASSWORD` and `TD_EGRESS_PROXY`; the web `app` service never gets a data volume.
- Paper trading needs `TD_MODE=PAPER` outside production; production refuses PAPER.
- Fees must be attested (`fees.operator_maker_rate`, `fees.attested_on`) or backtests and paper start refuse.

## Phase 6 additions
- `TD_REVIEW_DIR` (`review.dir`, default `/review`): where review packages live. Refused if relative, containing `..`, containing `static`, `web`, `public` or `templates`, or under `/app`. `review.max_bytes` (default 20 MiB, at most 50 MiB), `review.max_rows_per_file` (default 20000), `review.max_period_days` (at most 90).
- Whether the feature is on is a database flag (DISABLED by default) changed only by the ADMIN confirmation chain, not by configuration; retention days are chosen when enabling (1-90).
- The `app` service mounts the `review_packages` volume read-only; only `batch` mounts it read-write.
