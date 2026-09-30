-- Development/test rollback of 0006_safety.sql. DESTROYS control state, intents, attempts,
-- decisions, reconciliation records, hints and commands. Audit events stay.
DROP TABLE IF EXISTS control_commands;
DROP TABLE IF EXISTS api_events;
DROP TABLE IF EXISTS order_hints;
DROP TABLE IF EXISTS attempt_fills;
DROP TABLE IF EXISTS order_attempts CASCADE;
DROP TABLE IF EXISTS risk_decisions CASCADE;
DROP TABLE IF EXISTS order_intents CASCADE;
DROP TABLE IF EXISTS venue_baselines;
DROP TABLE IF EXISTS reconciliation_findings;
DROP TABLE IF EXISTS reconciliation_runs CASCADE;
DROP TABLE IF EXISTS bot_control_history;
DROP TABLE IF EXISTS bot_control CASCADE;
DROP FUNCTION IF EXISTS control_command_guard();
DROP FUNCTION IF EXISTS api_events_guard();
DROP FUNCTION IF EXISTS order_attempt_guard();
DROP FUNCTION IF EXISTS risk_decision_guard();
DROP FUNCTION IF EXISTS td_host_only();
DROP FUNCTION IF EXISTS bot_control_history_guard();
DROP FUNCTION IF EXISTS bot_control_record();
DROP FUNCTION IF EXISTS bot_control_guard();
DROP FUNCTION IF EXISTS td_fresh_reconciliation(timestamptz, timestamptz, text);
DROP FUNCTION IF EXISTS td_authorize_order(uuid, timestamptz);
DROP FUNCTION IF EXISTS reconciliation_run_guard();
DROP FUNCTION IF EXISTS td_check_time(timestamptz);
DROP FUNCTION IF EXISTS td_now();
DROP TABLE IF EXISTS td_test_clock;
