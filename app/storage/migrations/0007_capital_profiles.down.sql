-- Development/test rollback of 0007_capital_profiles.sql. Restores the fixed 50/15/35/12 limits.
-- Fails if an intent above 12 or a grid cell above 35 exists (they are not representable again).
DROP TRIGGER IF EXISTS paper_ledger_deposit_guard ON paper_ledger_entries;
DROP FUNCTION IF EXISTS paper_deposit_guard();
ALTER TABLE paper_ledger_entries DROP CONSTRAINT paper_ledger_deposit_is_positive;
ALTER TABLE paper_ledger_entries ADD CONSTRAINT paper_ledger_deposit_is_the_policy_capital
    CHECK (kind <> 'DEPOSIT' OR (quote_delta = 50 AND base_delta = 0));
ALTER TABLE grid_plans DROP CONSTRAINT grid_plans_cell_budget_check;
ALTER TABLE grid_plans ADD CONSTRAINT grid_plans_cell_budget_check CHECK (cell_budget > 0 AND cell_budget <= 35);
ALTER TABLE order_intents DROP CONSTRAINT order_intents_cap;
ALTER TABLE order_intents ADD CONSTRAINT order_intents_cap CHECK (price * base_qty <= 12);
DELETE FROM bot_control_history WHERE event IN ('PAPER_PROFILE', 'LIVE_PROFILE');
ALTER TABLE bot_control_history DROP CONSTRAINT bot_control_history_event_check;
ALTER TABLE bot_control_history ADD CONSTRAINT bot_control_history_event_check CHECK (event IN (
    'PAUSE', 'KILL_ACTIVATE', 'KILL_RELEASE', 'BREAKER_OPEN', 'RESUME', 'RECOVERY_INCOMPLETE',
    'RECOVERY_COMPLETE'));
ALTER TABLE bot_control DISABLE TRIGGER bot_control_guard_trigger;
ALTER TABLE bot_control DISABLE TRIGGER bot_control_record_trigger;
UPDATE bot_control SET paper_profile = 'pilot', live_profile = 'pilot';
ALTER TABLE bot_control ENABLE TRIGGER bot_control_record_trigger;
ALTER TABLE bot_control ENABLE TRIGGER bot_control_guard_trigger;
CREATE OR REPLACE FUNCTION bot_control_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    actor text := td_actor_class();
    is_pause boolean; is_resume boolean; kill_on boolean; kill_off boolean;
    tripped boolean; brk_close boolean; rec_reset boolean; rec_done boolean;
    unknowns integer;
    p_now timestamptz := td_now();
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bot control is never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF actor IS NULL THEN
        RAISE EXCEPTION 'unknown database role for bot control' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    PERFORM td_check_time(NEW.updated_at);
    IF NEW.id <> OLD.id OR NEW.version <> OLD.version + 1 OR NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'bot control changes advance the version by one and never go back in time'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    is_pause := OLD.bot_state = 'RUNNING' AND NEW.bot_state = 'PAUSED';
    is_resume := OLD.bot_state = 'PAUSED' AND NEW.bot_state = 'RUNNING';
    kill_on := OLD.kill_switch = 'INACTIVE' AND NEW.kill_switch = 'ACTIVE';
    kill_off := OLD.kill_switch = 'ACTIVE' AND NEW.kill_switch = 'INACTIVE';
    tripped := OLD.breaker_state = 'CLOSED' AND NEW.breaker_state = 'OPEN';
    brk_close := OLD.breaker_state = 'OPEN' AND NEW.breaker_state = 'CLOSED';
    rec_reset := OLD.recovery_state = 'COMPLETE' AND NEW.recovery_state = 'INCOMPLETE';
    rec_done := OLD.recovery_state = 'INCOMPLETE' AND NEW.recovery_state = 'COMPLETE';

    -- columns change only together with the transition that owns them
    IF (NEW.kill_reason IS DISTINCT FROM OLD.kill_reason OR NEW.kill_activated_at IS DISTINCT FROM OLD.kill_activated_at)
       AND NOT (kill_on OR kill_off) THEN
        RAISE EXCEPTION 'kill switch details change only when it is activated or released' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.breaker_reason IS DISTINCT FROM OLD.breaker_reason OR NEW.breaker_opened_at IS DISTINCT FROM OLD.breaker_opened_at
        OR NEW.breaker_cooldown_until IS DISTINCT FROM OLD.breaker_cooldown_until)
       AND NOT (tripped OR brk_close) THEN
        RAISE EXCEPTION 'breaker details change only when it opens or closes' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.recovery_completed_at IS DISTINCT FROM OLD.recovery_completed_at AND NOT (rec_done OR rec_reset) THEN
        RAISE EXCEPTION 'recovery time changes only with recovery state' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.boot_id IS DISTINCT FROM OLD.boot_id AND NOT (rec_reset OR rec_done OR OLD.recovery_state = 'INCOMPLETE') THEN
        RAISE EXCEPTION 'boot id changes only during recovery' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT (is_pause OR is_resume OR kill_on OR kill_off OR tripped OR brk_close OR rec_reset OR rec_done
            OR (NEW.boot_id IS DISTINCT FROM OLD.boot_id)) THEN
        RAISE EXCEPTION 'bot control update changes nothing recognised' USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- who may do what
    IF (tripped OR kill_off OR rec_reset OR rec_done OR NEW.boot_id IS DISTINCT FROM OLD.boot_id) AND actor <> 'HOST' THEN
        RAISE EXCEPTION 'only the host changes breaker, recovery and releases the kill switch' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (is_resume OR brk_close) AND actor <> 'WEB' THEN
        RAISE EXCEPTION 'only the ADMIN dashboard resumes the bot' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF brk_close AND NOT is_resume THEN
        RAISE EXCEPTION 'the breaker closes only together with a resume' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (tripped OR kill_on OR rec_reset) AND NEW.bot_state <> 'PAUSED' THEN
        RAISE EXCEPTION 'a restrictive change pauses the bot' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (kill_off OR rec_done) AND is_resume THEN
        RAISE EXCEPTION 'releasing or completing recovery never resumes the bot' USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF rec_done THEN
        IF NOT td_fresh_reconciliation(p_now, NULL) THEN
            RAISE EXCEPTION 'recovery completes only after a current successful reconciliation' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.recovery_completed_at IS NULL THEN
            RAISE EXCEPTION 'recovery completion needs its time' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    IF is_resume THEN
        IF NEW.kill_switch <> 'INACTIVE' OR NEW.recovery_state <> 'COMPLETE'
           OR NEW.breaker_state <> 'CLOSED' THEN
            RAISE EXCEPTION 'resume needs kill switch inactive, breaker closed and recovery complete' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.breaker_state = 'OPEN' AND OLD.breaker_cooldown_until > p_now THEN
            RAISE EXCEPTION 'the breaker cooldown has not elapsed' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT td_fresh_reconciliation(p_now, CASE WHEN OLD.breaker_state = 'OPEN' THEN OLD.breaker_opened_at END) THEN
            RAISE EXCEPTION 'resume needs a current successful reconciliation' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT count(*) INTO unknowns FROM order_attempts WHERE state = 'UNKNOWN';
        IF unknowns > 0 THEN
            RAISE EXCEPTION 'resume is blocked while an order attempt has an unknown outcome' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

CREATE OR REPLACE FUNCTION bot_control_record() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE ev text;
BEGIN
    ev := CASE
        WHEN OLD.kill_switch = 'INACTIVE' AND NEW.kill_switch = 'ACTIVE' THEN 'KILL_ACTIVATE'
        WHEN OLD.kill_switch = 'ACTIVE' AND NEW.kill_switch = 'INACTIVE' THEN 'KILL_RELEASE'
        WHEN OLD.breaker_state = 'CLOSED' AND NEW.breaker_state = 'OPEN' THEN 'BREAKER_OPEN'
        WHEN OLD.bot_state = 'PAUSED' AND NEW.bot_state = 'RUNNING' THEN 'RESUME'
        WHEN OLD.bot_state = 'RUNNING' AND NEW.bot_state = 'PAUSED' THEN 'PAUSE'
        WHEN OLD.recovery_state = 'COMPLETE' AND NEW.recovery_state = 'INCOMPLETE' THEN 'RECOVERY_INCOMPLETE'
        WHEN OLD.recovery_state = 'INCOMPLETE' AND NEW.recovery_state = 'COMPLETE' THEN 'RECOVERY_COMPLETE'
        ELSE NULL END;
    IF ev IS NOT NULL THEN
        INSERT INTO bot_control_history (occurred_at, actor_class, event, bot_state, kill_switch, breaker_state,
                                         recovery_state, reason, version)
        VALUES (NEW.updated_at, td_actor_class(), ev, NEW.bot_state, NEW.kill_switch, NEW.breaker_state,
                NEW.recovery_state, NEW.last_change_reason, NEW.version);
    END IF;
    RETURN NEW;
END
$fn$;

CREATE OR REPLACE FUNCTION td_authorize_order(p_intent uuid, p_now timestamptz) RETURNS text
    LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public AS
$fn$
DECLARE
    i order_intents%ROWTYPE;
    baseline numeric;
    cash numeric;
    reserved_buys numeric;
    reserved_sell_qty numeric;
    inv_cost_ub numeric;
    inv_qty numeric;
    notional numeric;
    outlay numeric;
    cur_eq numeric;
    peak numeric;
    day_open numeric;
    day_start timestamptz := date_trunc('day', p_now AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
BEGIN
    SELECT * INTO i FROM order_intents WHERE id = p_intent;
    IF NOT FOUND THEN
        RETURN 'INTENT_MISSING';
    END IF;
    SELECT amount INTO baseline FROM venue_baselines WHERE venue = i.venue AND currency = 'USDC';
    IF baseline IS NULL THEN
        RETURN 'NO_BASELINE';
    END IF;
    notional := i.price * i.base_qty;
    IF notional > 12 THEN
        RETURN 'ORDER_CAP_BREACH';
    END IF;

    SELECT baseline + COALESCE(sum(CASE f.side WHEN 'BUY' THEN -(f.price * f.size + f.fee)
                                                ELSE f.price * f.size - f.fee END), 0)
        INTO cash FROM attempt_fills f WHERE f.venue = i.venue;

    SELECT COALESCE(sum(CASE WHEN oi.side = 'BUY' THEN GREATEST(oi.base_qty - a.filled_qty, 0) * oi.price END), 0),
           COALESCE(sum(CASE WHEN oi.side = 'SELL' AND oi.product_id = i.product_id
                             THEN GREATEST(oi.base_qty - a.filled_qty, 0) END), 0)
        INTO reserved_buys, reserved_sell_qty
        FROM order_attempts a JOIN order_intents oi ON oi.id = a.intent_id
        WHERE oi.venue = i.venue
          AND a.state IN ('AUTHORIZED', 'SUBMITTING', 'WORKING', 'CANCEL_REQUESTED', 'UNKNOWN');

    WITH pos AS (
        SELECT oi.product_id,
               sum(CASE f.side WHEN 'BUY' THEN f.size ELSE -f.size END) AS qty,
               max(CASE WHEN f.side = 'BUY' THEN f.price END) AS max_buy
        FROM attempt_fills f
        JOIN order_attempts a ON a.id = f.attempt_id
        JOIN order_intents oi ON oi.id = a.intent_id
        WHERE f.venue = i.venue GROUP BY oi.product_id)
    SELECT COALESCE(sum(GREATEST(qty, 0) * COALESCE(max_buy, 0)), 0),
           COALESCE(max(CASE WHEN product_id = i.product_id THEN GREATEST(qty, 0) END), 0)
        INTO inv_cost_ub, inv_qty FROM pos;

    IF i.side = 'BUY' THEN
        outlay := notional * 1.006;  -- the stress maker fee (0.60%)
        IF cash - reserved_buys - outlay < 15 THEN
            RETURN 'RESERVE_BREACH';
        END IF;
        IF reserved_buys + inv_cost_ub + notional > 35 THEN
            RETURN 'DEPLOYMENT_CAP_BREACH';
        END IF;
    ELSIF i.base_qty > inv_qty - reserved_sell_qty THEN
        RETURN 'SELL_EXCEEDS_INVENTORY';
    END IF;

    -- equity is sampled at each recorded fill, marking every held product at its latest fill price
    WITH f AS (
        SELECT fl.id, fl.occurred_at, fl.side, fl.price, fl.size, fl.fee, oi.product_id
        FROM attempt_fills fl
        JOIN order_attempts a ON a.id = fl.attempt_id
        JOIN order_intents oi ON oi.id = a.intent_id
        WHERE fl.venue = i.venue),
    pts AS (
        SELECT f1.id, f1.occurred_at,
               baseline
               + (SELECT COALESCE(sum(CASE f2.side WHEN 'BUY' THEN -(f2.price * f2.size + f2.fee)
                                                  ELSE f2.price * f2.size - f2.fee END), 0)
                    FROM f f2 WHERE (f2.occurred_at, f2.id) <= (f1.occurred_at, f1.id))
               + (SELECT COALESCE(sum(q.qty * q.mark), 0) FROM (
                      SELECT f3.product_id,
                             sum(CASE f3.side WHEN 'BUY' THEN f3.size ELSE -f3.size END) AS qty,
                             (array_agg(f3.price ORDER BY f3.occurred_at DESC, f3.id DESC))[1] AS mark
                      FROM f f3 WHERE (f3.occurred_at, f3.id) <= (f1.occurred_at, f1.id)
                      GROUP BY f3.product_id) q WHERE q.qty > 0) AS equity
        FROM f f1)
    SELECT (SELECT equity FROM pts ORDER BY occurred_at DESC, id DESC LIMIT 1),
           GREATEST(baseline, COALESCE((SELECT max(equity) FROM pts), baseline)),
           COALESCE((SELECT equity FROM pts WHERE occurred_at < day_start
                     ORDER BY occurred_at DESC, id DESC LIMIT 1), baseline)
        INTO cur_eq, peak, day_open;
    cur_eq := COALESCE(cur_eq, baseline);
    IF day_open - cur_eq >= 10 THEN
        RETURN 'LOSS_LIMIT';
    END IF;
    IF peak > 0 AND (peak - cur_eq) / peak >= 0.20 THEN
        RETURN 'DRAWDOWN_LIMIT';
    END IF;
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION paper_capital_check() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    cash numeric;
    reserved numeric;
    cost numeric;
BEGIN
    SELECT COALESCE(sum(quote_delta), 0) INTO cash FROM paper_ledger_entries;
    SELECT COALESCE(sum(quote_reserved), 0) INTO reserved FROM paper_orders WHERE state = 'OPEN';
    SELECT COALESCE(sum(cost_basis), 0) INTO cost FROM paper_positions;
    IF cash > 0 AND cash - reserved < 15 THEN
        RAISE EXCEPTION 'paper reserve breached: free cash below 15' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF reserved + cost > 35 THEN
        RAISE EXCEPTION 'paper deployment cap breached: above 35' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END
$fn$;

ALTER TABLE bot_control DROP COLUMN live_profile, DROP COLUMN paper_profile;
DROP FUNCTION IF EXISTS td_capital_profile(text);
DROP TABLE IF EXISTS capital_profiles;
