-- Development/test rollback of 0011_trading_mode.sql.
ALTER TABLE trading_state DISABLE TRIGGER trading_state_guard_trigger;
UPDATE trading_state SET active_mode = 'PAPER' WHERE active_mode = 'BACKTEST';
ALTER TABLE trading_state ENABLE TRIGGER trading_state_guard_trigger;
ALTER TABLE trading_state DROP CONSTRAINT trading_state_active_mode_check;
ALTER TABLE trading_state ADD CONSTRAINT trading_state_active_mode_check CHECK (active_mode IN ('PAPER', 'LIVE'));
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
            JOIN trading_state s ON s.active_mode = c.mode WHERE s.id;
        SELECT count(*) INTO active FROM pairs WHERE state = 'PAPER_ACTIVE' AND id <> NEW.id;
        IF active >= COALESCE(allowed, 1) THEN
            RAISE EXCEPTION 'the configured number of active pairs is already reached' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;
