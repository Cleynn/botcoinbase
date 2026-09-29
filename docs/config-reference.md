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

## Phase 7 additions
- `TD_PROPOSAL_DIR` (`proposals.dir`, default `/proposals`): where imported proposal files live. Refused if relative, containing `..`, containing `static`, `web`, `public` or `templates`, or under `/app`. `proposals.max_bytes` (default 128 KiB), `proposals.max_depth` (6), `proposals.retention_days` (90).
- Whether import is on is a database flag (DISABLED by default) changed only by the ADMIN confirmation chain.
- The `proposals` volume is mounted read-write on `app` and `batch` only.

## Phase 8 additions
`safety:` in the YAML config (`SafetySettings`): `market_data_max_age_seconds` (900), `metadata_max_age_seconds` (3600), `book_max_age_seconds` (60), `max_spread_bps` (30), `max_price_deviation_ratio` (0.05), `reconcile_max_age_seconds` (300, at most 300), `api_failure_window_seconds`/`api_failure_threshold` (300/5), `breaker_cooldown_seconds` (900), `daily_loss_limit` (3 USDC, at most 10), `max_drawdown_ratio` (0.10, at most 0.20), `per_order_cap` (at most the 12 USDC ceiling), `max_intents_per_minute` (10), `max_reject_streak` (5), `retry_max_attempts` (3, at most 5), `retry_base_seconds`, `retry_cap_seconds`, `absence_window_seconds` (120, at least 30; the database floor is 120), `absence_min_reconciliations` (2), `ws_max_silence_seconds` (30). No setting can enable live trading and there is no credential setting.
