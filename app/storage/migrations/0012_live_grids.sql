-- Phase 10d: the grids the live runner manages. One row per grid: the frozen plan (lines and cell
-- sizes) so the runner can resume after a restart, and a state. The web role may only read; the
-- host writes. A grid is never deleted. Only one grid per pair is ACTIVE at a time.
CREATE TABLE live_grids (
    id          uuid        PRIMARY KEY,
    pair_id     uuid        NOT NULL REFERENCES pairs (id) ON DELETE RESTRICT,
    product_id  text        NOT NULL CHECK (product_id ~ '^[A-Z0-9]+-USDC$'),
    state       text        NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'STOPPED')),
    levels      integer     NOT NULL CHECK (levels BETWEEN 3 AND 20),
    lower       numeric     NOT NULL CHECK (lower > 0),
    upper       numeric     NOT NULL CHECK (upper > lower),
    cells       jsonb       NOT NULL CHECK (jsonb_typeof(cells) = 'array' AND jsonb_array_length(cells) BETWEEN 2 AND 19),
    commitment  numeric     NOT NULL CHECK (commitment >= 0),
    created_at  timestamptz NOT NULL,
    stopped_at  timestamptz,
    stop_reason text        CHECK (stop_reason IS NULL OR stop_reason ~ '^[A-Z_]{3,40}$'),
    CONSTRAINT live_grids_stop_consistent CHECK ((state = 'STOPPED') = (stopped_at IS NOT NULL))
);
CREATE UNIQUE INDEX live_grids_one_active_per_pair ON live_grids (pair_id) WHERE state = 'ACTIVE';
CREATE INDEX live_grids_pair_idx ON live_grids (pair_id, created_at DESC);

CREATE FUNCTION live_grids_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'live grids are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS DISTINCT FROM 'HOST' THEN
        RAISE EXCEPTION 'only the host writes live grids' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.state = 'STOPPED' OR NEW.state <> 'STOPPED'
           OR NEW.id <> OLD.id OR NEW.pair_id <> OLD.pair_id OR NEW.cells::text <> OLD.cells::text
           OR NEW.lower <> OLD.lower OR NEW.upper <> OLD.upper OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'a live grid may only go from ACTIVE to STOPPED' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        PERFORM td_check_time(NEW.stopped_at);
    ELSE
        PERFORM td_check_time(NEW.created_at);
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER live_grids_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON live_grids
    FOR EACH ROW EXECUTE FUNCTION live_grids_guard();
CREATE TRIGGER live_grids_no_truncate BEFORE TRUNCATE ON live_grids
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

GRANT SELECT ON live_grids TO td_app, td_ctl;
GRANT INSERT ON live_grids TO td_ctl;
GRANT UPDATE (state, stopped_at, stop_reason) ON live_grids TO td_ctl;
