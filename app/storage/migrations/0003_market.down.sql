-- Development/test rollback of 0003_market.sql. DESTROYS candles, snapshots' database rows, backtests,
-- reports and the paper ledger. Parquet files on disk are NOT removed (delete them by hand).
-- Audit events already written are append-only and stay.
DROP TABLE IF EXISTS paper_fills;
DROP TABLE IF EXISTS paper_ledger_entries;
DROP TABLE IF EXISTS paper_positions;
DROP TABLE IF EXISTS paper_orders CASCADE;
DROP TABLE IF EXISTS paper_session;
DROP TABLE IF EXISTS grid_plans;
DROP TABLE IF EXISTS reports;
DROP TABLE IF EXISTS backtest_runs;
DROP TABLE IF EXISTS dataset_snapshots;
DROP TABLE IF EXISTS ingest_cursors;
DROP TABLE IF EXISTS data_quality_events;
DROP TABLE IF EXISTS candle_conflicts;
DROP TABLE IF EXISTS candles;
DROP TABLE IF EXISTS ingest_runs;
DROP FUNCTION IF EXISTS paper_capital_check();
DROP FUNCTION IF EXISTS paper_order_guard();
DROP FUNCTION IF EXISTS ingest_cursor_guard();
