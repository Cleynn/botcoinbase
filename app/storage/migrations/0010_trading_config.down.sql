-- Development/test rollback of 0010_trading_config.sql (restores the fixed limits and one active pair).
-- Fails if more than one pair is active or an intent above 50 USDC exists.
CREATE OR REPLACE FUNCTION live_arming_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    p_now timestamptz := td_now();
    ctl bot_control%ROWTYPE;
    need text;
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
    FOREACH need IN ARRAY ARRAY['INDEPENDENT_REVIEW_DONE', 'EXCHANGE_CONTRACT_REVIEWED', 'SIGNED_PILOT_REVIEW', 'SOAK_COMPLETE', 'DEC000_ACKNOWLEDGED', 'TRADABILITY_CONFIRMED', 'KEY_SCOPE_CONFIRMED'] LOOP
        IF NOT EXISTS (SELECT 1 FROM live_attestations a
                       WHERE a.code = need AND a.attested_at >= NEW.armed_at - interval '30 days') THEN
            RAISE EXCEPTION 'attestation % is missing or older than 30 days', need USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END LOOP;
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

ALTER TABLE grid_plans DROP CONSTRAINT grid_plans_cell_budget_check;
ALTER TABLE grid_plans ADD CONSTRAINT grid_plans_cell_budget_check CHECK (cell_budget > 0 AND cell_budget <= 150);
ALTER TABLE order_intents DROP CONSTRAINT order_intents_cap;
ALTER TABLE order_intents ADD CONSTRAINT order_intents_cap CHECK (price * base_qty <= 50);
ALTER TABLE order_intents ADD CONSTRAINT order_intents_live_cap CHECK (venue <> 'COINBASE' OR price * base_qty <= 12);
DROP TRIGGER IF EXISTS pairs_max_active_trigger ON pairs;
DROP FUNCTION IF EXISTS pairs_max_active_guard();
CREATE UNIQUE INDEX pairs_one_active ON pairs ((true)) WHERE state IN ('PAPER_ACTIVE', 'LIVE_ACTIVE');
DROP TRIGGER IF EXISTS bot_control_apply_preset_trigger ON bot_control;
DROP FUNCTION IF EXISTS bot_control_apply_preset();
UPDATE bot_control SET paper_profile = 'pilot' WHERE paper_profile = 'custom';
UPDATE bot_control SET live_profile = 'pilot' WHERE live_profile = 'custom';
ALTER TABLE capital_profiles DISABLE TRIGGER capital_profiles_immutable;
DELETE FROM capital_profiles WHERE name = 'custom';
ALTER TABLE capital_profiles ENABLE TRIGGER capital_profiles_immutable;

CREATE OR REPLACE FUNCTION td_capital_profile(p_mode text) RETURNS capital_profiles
    LANGUAGE sql STABLE SET search_path = pg_catalog, public AS
$fn$
    SELECT p.* FROM capital_profiles p JOIN bot_control c ON c.id
    WHERE p.name = CASE p_mode WHEN 'LIVE' THEN c.live_profile WHEN 'PAPER' THEN c.paper_profile END
$fn$;
DROP TABLE IF EXISTS trading_state;
DROP TABLE IF EXISTS trading_config;
DROP FUNCTION IF EXISTS trading_state_guard();
DROP FUNCTION IF EXISTS trading_config_guard();
DROP FUNCTION IF EXISTS td_paper_side_idle();
