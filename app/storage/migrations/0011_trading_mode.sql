-- Phase 10c: the active trading mode can be BACKTEST, PAPER or LIVE (it was PAPER or LIVE).
-- Choosing a mode never starts anything and never places an order: it records what the host services
-- should run. LIVE orders still need a host arming (migration 0009) and the kill switch, reserve and
-- caps still apply. BACKTEST and PAPER use the PAPER configuration row.
ALTER TABLE trading_state DROP CONSTRAINT trading_state_active_mode_check;
ALTER TABLE trading_state ADD CONSTRAINT trading_state_active_mode_check
    CHECK (active_mode IN ('BACKTEST', 'PAPER', 'LIVE'));

CREATE OR REPLACE FUNCTION pairs_max_active_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    allowed integer;
    active integer;
BEGIN
    IF NEW.state = 'PAPER_ACTIVE' AND (TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM NEW.state) THEN
        PERFORM pg_advisory_xact_lock(hashtext('td_active_pairs'));
        SELECT c.max_pairs INTO allowed FROM trading_config c
            JOIN trading_state s ON c.mode = CASE WHEN s.active_mode = 'LIVE' THEN 'LIVE' ELSE 'PAPER' END
            WHERE s.id;
        SELECT count(*) INTO active FROM pairs WHERE state = 'PAPER_ACTIVE' AND id <> NEW.id;
        IF active >= COALESCE(allowed, 1) THEN
            RAISE EXCEPTION 'the configured number of active pairs is already reached' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;
