-- Phase 10b: one editable trading configuration per mode (PAPER, LIVE), the active mode, and more
-- than one active pair. The fund protections stay: td_authorize_order and the paper capital triggers
-- keep enforcing reserve, per-order cap and invested cap, now read from trading_config through
-- td_capital_profile(); the kill switch, breaker and control guards are unchanged.
--
-- RELAXED by the owner's decision (DEC-026), to be re-enabled later: the 12 USDC cap on COINBASE
-- intents, the fixed 3-5 grid lines, the single active pair, and the human attestations that arming
-- used to require.

-- ------------------------------------------------------------------ the editable configuration
CREATE TABLE trading_config (
    mode            text        PRIMARY KEY CHECK (mode IN ('PAPER', 'LIVE')),
    max_pairs       integer     NOT NULL CHECK (max_pairs BETWEEN 1 AND 10),
    levels_per_grid integer     NOT NULL CHECK (levels_per_grid BETWEEN 3 AND 20),
    quote_per_grid  numeric     NOT NULL CHECK (quote_per_grid >= 0),
    invested_cap    numeric     NOT NULL CHECK (invested_cap >= 0),
    reserve         numeric     NOT NULL CHECK (reserve >= 0),
    per_order_cap   numeric     NOT NULL CHECK (per_order_cap >= 0),
    version         integer     NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at      timestamptz NOT NULL,
    CONSTRAINT trading_grids_fit_the_cap CHECK (quote_per_grid * max_pairs <= invested_cap),
    CONSTRAINT trading_order_fits_the_grid CHECK (per_order_cap <= quote_per_grid)
);
INSERT INTO trading_config (mode, max_pairs, levels_per_grid, quote_per_grid, invested_cap, reserve, per_order_cap, updated_at)
VALUES ('PAPER', 1, 3, 35, 35, 15, 12, 'epoch'), ('LIVE', 1, 3, 35, 35, 15, 12, 'epoch');

CREATE TABLE trading_state (
    id          boolean     PRIMARY KEY DEFAULT true CHECK (id),
    active_mode text        NOT NULL DEFAULT 'PAPER' CHECK (active_mode IN ('PAPER', 'LIVE')),
    version     integer     NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at  timestamptz NOT NULL
);
INSERT INTO trading_state (id, updated_at) VALUES (true, 'epoch');

-- The paper side must be idle (the paper trader does not follow bot_state) before anything that
-- changes its limits or the active mode.
CREATE FUNCTION td_paper_side_idle() RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, public AS
$fn$
    SELECT COALESCE((SELECT state FROM paper_session WHERE id), 'RUNNING') = 'PAUSED'
       AND NOT EXISTS (SELECT 1 FROM paper_orders WHERE state = 'OPEN')
$fn$;

CREATE FUNCTION trading_config_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    pcash numeric;
    pcost numeric;
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'trading configuration rows are created by migration and never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS NULL THEN
        RAISE EXCEPTION 'unknown database role for trading configuration' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.mode <> OLD.mode OR NEW.version <> OLD.version + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'trading configuration changes advance the version by one and never go back in time' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    PERFORM td_check_time(NEW.updated_at);
    IF (SELECT bot_state FROM bot_control WHERE id) <> 'PAUSED' THEN
        RAISE EXCEPTION 'trading configuration changes only while the bot is PAUSED' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.mode = 'PAPER' THEN
        IF NOT td_paper_side_idle() THEN
            RAISE EXCEPTION 'the paper session must be PAUSED with no open paper orders to change its configuration' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT COALESCE(sum(quote_delta), 0) INTO pcash FROM paper_ledger_entries;
        SELECT COALESCE(sum(cost_basis), 0) INTO pcost FROM paper_positions;
        IF pcash > 0 AND (pcash < NEW.reserve OR pcost > NEW.invested_cap) THEN
            RAISE EXCEPTION 'existing paper state exceeds the new configuration limits' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER trading_config_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON trading_config
    FOR EACH ROW EXECUTE FUNCTION trading_config_guard();
CREATE TRIGGER trading_config_no_truncate BEFORE TRUNCATE ON trading_config
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

CREATE FUNCTION trading_state_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'trading state is created by migration and never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS NULL THEN
        RAISE EXCEPTION 'unknown database role for trading state' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.version <> OLD.version + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'trading state changes advance the version by one and never go back in time' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    PERFORM td_check_time(NEW.updated_at);
    IF NEW.active_mode <> OLD.active_mode THEN
        IF (SELECT bot_state FROM bot_control WHERE id) <> 'PAUSED' THEN
            RAISE EXCEPTION 'the trading mode changes only while the bot is PAUSED' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT td_paper_side_idle() THEN
            RAISE EXCEPTION 'the paper session must be PAUSED with no open paper orders to switch the trading mode' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER trading_state_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON trading_state
    FOR EACH ROW EXECUTE FUNCTION trading_state_guard();
CREATE TRIGGER trading_state_no_truncate BEFORE TRUNCATE ON trading_state
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

GRANT SELECT ON trading_config, trading_state TO td_app, td_ctl;
GRANT UPDATE (max_pairs, levels_per_grid, quote_per_grid, invested_cap, reserve, per_order_cap, version, updated_at)
    ON trading_config TO td_app, td_ctl;
GRANT UPDATE (active_mode, version, updated_at) ON trading_state TO td_app, td_ctl;

-- ------------------------------------------------------------------ the money limits come from it
-- Same row type as before, so td_authorize_order and the paper triggers are unchanged: the allocation
-- cap is invested + reserve, the reserve and invested cap are the configured ones.
CREATE OR REPLACE FUNCTION td_capital_profile(p_mode text) RETURNS capital_profiles
    LANGUAGE sql STABLE SET search_path = pg_catalog, public AS
$fn$
    SELECT 'custom'::text, 'Trading configuration'::text, c.invested_cap + c.reserve, c.reserve,
           c.invested_cap, c.per_order_cap
    FROM trading_config c WHERE c.mode = p_mode
$fn$;

-- 'custom' is the profile name of a hand-edited configuration
ALTER TABLE capital_profiles DISABLE TRIGGER capital_profiles_no_insert;
INSERT INTO capital_profiles (name, title, allocation_cap, protected_reserve, max_deployment, max_order)
VALUES ('custom', 'Custom trading configuration', 0, 0, 0, 0);
ALTER TABLE capital_profiles ENABLE TRIGGER capital_profiles_no_insert;

-- Choosing a preset profile copies its numbers into the configuration of that mode.
CREATE FUNCTION bot_control_apply_preset() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    p capital_profiles%ROWTYPE;
BEGIN
    IF NEW.paper_profile IS DISTINCT FROM OLD.paper_profile AND NEW.paper_profile <> 'custom' THEN
        SELECT * INTO p FROM capital_profiles WHERE name = NEW.paper_profile;
        UPDATE trading_config SET invested_cap = p.max_deployment, reserve = p.protected_reserve,
               per_order_cap = p.max_order,
               quote_per_grid = trunc(p.max_deployment / max_pairs, 8),
               version = version + 1, updated_at = NEW.updated_at
        WHERE mode = 'PAPER';
    END IF;
    IF NEW.live_profile IS DISTINCT FROM OLD.live_profile AND NEW.live_profile <> 'custom' THEN
        SELECT * INTO p FROM capital_profiles WHERE name = NEW.live_profile;
        UPDATE trading_config SET invested_cap = p.max_deployment, reserve = p.protected_reserve,
               per_order_cap = p.max_order,
               quote_per_grid = trunc(p.max_deployment / max_pairs, 8),
               version = version + 1, updated_at = NEW.updated_at
        WHERE mode = 'LIVE';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER bot_control_apply_preset_trigger AFTER UPDATE ON bot_control
    FOR EACH ROW EXECUTE FUNCTION bot_control_apply_preset();

-- ------------------------------------------------------------------ more than one active pair
DROP INDEX pairs_one_active;
CREATE FUNCTION pairs_max_active_guard() RETURNS trigger
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
CREATE TRIGGER pairs_max_active_trigger BEFORE INSERT OR UPDATE ON pairs
    FOR EACH ROW EXECUTE FUNCTION pairs_max_active_guard();

-- ------------------------------------------------------------------ relaxed limits (DEC-026)
ALTER TABLE order_intents DROP CONSTRAINT order_intents_live_cap;
ALTER TABLE order_intents DROP CONSTRAINT order_intents_cap;
ALTER TABLE order_intents ADD CONSTRAINT order_intents_cap CHECK (price * base_qty <= 1000000);
ALTER TABLE grid_plans DROP CONSTRAINT grid_plans_cell_budget_check;
ALTER TABLE grid_plans ADD CONSTRAINT grid_plans_cell_budget_check CHECK (cell_budget > 0);

CREATE OR REPLACE FUNCTION live_arming_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    p_now timestamptz := td_now();
    ctl bot_control%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'live arming records are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS DISTINCT FROM 'HOST' THEN
        RAISE EXCEPTION 'live arming is made on the host only' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.id <> OLD.id OR NEW.armed_at <> OLD.armed_at OR NEW.expires_at <> OLD.expires_at
           OR NEW.armed_by <> OLD.armed_by OR NEW.key_hint <> OLD.key_hint OR NEW.checks <> OLD.checks
           OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL THEN
            RAISE EXCEPTION 'an arming can only be revoked, once' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        PERFORM td_check_time(NEW.revoked_at);
        RETURN NEW;
    END IF;
    PERFORM td_check_time(NEW.armed_at);
    IF NEW.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'a new arming is not revoked' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM live_arming WHERE revoked_at IS NULL AND expires_at > p_now) THEN
        RAISE EXCEPTION 'an arming is already active' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO ctl FROM bot_control WHERE id FOR SHARE;
    IF ctl.kill_switch <> 'INACTIVE' OR ctl.breaker_state <> 'CLOSED' OR ctl.recovery_state <> 'COMPLETE' THEN
        RAISE EXCEPTION 'arming needs the kill switch inactive, the breaker closed and recovery complete' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM venue_baselines WHERE venue = 'COINBASE' AND currency = 'USDC') THEN
        RAISE EXCEPTION 'arming needs a recorded USDC baseline for COINBASE' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT td_fresh_reconciliation(p_now, NULL, 'COINBASE') THEN
        RAISE EXCEPTION 'arming needs a current successful COINBASE reconciliation' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
