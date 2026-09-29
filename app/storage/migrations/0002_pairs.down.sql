-- Development/test rollback of 0002_pairs.sql. DESTROYS products, pairs, history and validation
-- evidence. Audit events already written are append-only and are NOT removed.
DROP TABLE IF EXISTS pair_state_history;
DROP TABLE IF EXISTS pairs CASCADE;
DROP TABLE IF EXISTS pair_validation_runs;
DROP TABLE IF EXISTS allowed_transitions;
DROP TABLE IF EXISTS product_metadata_current;
DROP TABLE IF EXISTS product_metadata_snapshots;
DROP TABLE IF EXISTS products;
DROP INDEX IF EXISTS audit_events_target_idx;
DROP FUNCTION IF EXISTS pairs_require_history();
DROP FUNCTION IF EXISTS pair_state_history_guard();
DROP FUNCTION IF EXISTS pairs_guard();
DROP FUNCTION IF EXISTS td_actor_class();
DROP FUNCTION IF EXISTS td_append_only();
