CREATE OR REPLACE FUNCTION td_live_armed(p_now timestamptz) RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, public AS
$fn$
    SELECT EXISTS (
        SELECT 1 FROM live_arming a
        WHERE a.revoked_at IS NULL AND a.armed_at <= p_now AND a.expires_at > p_now
          AND NOT EXISTS (
              SELECT 1 FROM bot_control_history h
              WHERE h.occurred_at >= a.armed_at
                AND h.event IN ('KILL_ACTIVATE', 'BREAKER_OPEN', 'RECOVERY_INCOMPLETE', 'LIVE_PROFILE')))
$fn$;
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
DROP TABLE IF EXISTS live_grids;
DROP FUNCTION IF EXISTS live_grids_guard();
