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
DROP TABLE IF EXISTS live_grids;
DROP FUNCTION IF EXISTS live_grids_guard();
