-- Development/test rollback of 0009_live_venue.sql. Destroys attestations and arming records and
-- fails if any COINBASE row exists (the venue is no longer representable).
CREATE OR REPLACE FUNCTION td_authorize_order(p_intent uuid, p_now timestamptz) RETURNS text
    LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public AS
$fn$
DECLARE
    i order_intents%ROWTYPE;
    prof capital_profiles%ROWTYPE;
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
    prof := td_capital_profile('PAPER');  -- every representable venue is a paper venue
    IF prof.name IS NULL THEN
        RETURN 'NO_PROFILE';
    END IF;
    notional := i.price * i.base_qty;
    IF notional > prof.max_order THEN
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
        IF cash - reserved_buys - outlay < prof.protected_reserve THEN
            RETURN 'RESERVE_BREACH';
        END IF;
        IF reserved_buys + inv_cost_ub + notional > prof.max_deployment THEN
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

CREATE OR REPLACE FUNCTION order_attempt_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    ctl bot_control%ROWTYPE;
    dec risk_decisions%ROWTYPE;
    prev integer;
    bad integer;
    proofs integer;
    spread interval;
    refusal text;
    p_now timestamptz := td_now();
    venue_ text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'order attempts are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS DISTINCT FROM 'HOST' THEN
        RAISE EXCEPTION 'order attempts are written by the host only' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'AUTHORIZED' OR NEW.submitting_at IS NOT NULL OR NEW.exchange_order_id IS NOT NULL OR NEW.filled_qty <> 0 THEN
            RAISE EXCEPTION 'an attempt starts AUTHORIZED and empty' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        -- FOR SHARE: a kill switch, pause or profile change (they update this row) waits for an
        -- authorization in flight, and an authorization waits for one already committing
        SELECT * INTO ctl FROM bot_control WHERE id FOR SHARE;
        IF ctl.kill_switch <> 'INACTIVE' OR ctl.breaker_state <> 'CLOSED' OR ctl.bot_state <> 'RUNNING'
           OR ctl.recovery_state <> 'COMPLETE' THEN
            RAISE EXCEPTION 'orders are not authorized while the bot is not RUNNING and clear' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        PERFORM td_check_time(NEW.created_at);
        IF ctl.boot_id IS NULL OR NEW.boot_id <> ctl.boot_id THEN
            RAISE EXCEPTION 'an attempt must carry the boot id of the recovery that started this process'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT venue INTO venue_ FROM order_intents WHERE id = NEW.intent_id;
        IF NOT td_fresh_reconciliation(p_now, NULL, venue_) THEN
            RAISE EXCEPTION 'orders need a current successful reconciliation' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM order_attempts WHERE state = 'UNKNOWN') THEN
            RAISE EXCEPTION 'orders are blocked while an attempt has an unknown outcome' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT * INTO dec FROM risk_decisions WHERE id = NEW.decision_id FOR UPDATE;
        IF NOT FOUND OR dec.decision <> 'ALLOW' OR dec.intent_id <> NEW.intent_id OR dec.consumed_at IS NOT NULL
           OR dec.expires_at < p_now OR dec.decided_at > p_now + interval '60 seconds' THEN
            RAISE EXCEPTION 'no valid unconsumed ALLOW decision for this attempt' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT count(*), count(*) FILTER (WHERE state NOT IN ('ABSENT', 'REJECTED'))
            INTO prev, bad FROM order_attempts WHERE intent_id = NEW.intent_id;
        IF bad > 0 THEN
            RAISE EXCEPTION 'a new attempt needs every earlier attempt absent or rejected' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.attempt_no <> prev + 1 THEN
            RAISE EXCEPTION 'attempt numbers are consecutive' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('td_order_authorize'));  -- money rules one at a time
        refusal := td_authorize_order(NEW.intent_id, p_now);
        IF refusal IS NOT NULL THEN
            RAISE EXCEPTION 'order refused by the database: %', refusal USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        UPDATE risk_decisions SET consumed_at = NEW.created_at WHERE id = NEW.decision_id AND consumed_at IS NULL;
        RETURN NEW;
    END IF;

    IF NEW.id <> OLD.id OR NEW.intent_id <> OLD.intent_id OR NEW.attempt_no <> OLD.attempt_no
       OR NEW.client_order_id <> OLD.client_order_id OR NEW.decision_id <> OLD.decision_id
       OR NEW.boot_id <> OLD.boot_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'attempt identity is immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.exchange_order_id IS NOT NULL AND NEW.exchange_order_id IS DISTINCT FROM OLD.exchange_order_id THEN
        RAISE EXCEPTION 'the exchange order id is write-once' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.submitting_at IS NOT NULL AND NEW.submitting_at IS DISTINCT FROM OLD.submitting_at THEN
        RAISE EXCEPTION 'the submit mark is write-once' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.filled_qty < OLD.filled_qty THEN
        RAISE EXCEPTION 'filled quantity never decreases' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = OLD.state THEN
        -- a settled attempt keeps its outcome; a late fill may still raise the filled quantity
        IF OLD.state IN ('FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED', 'ABSENT')
           AND NEW.failure_code IS DISTINCT FROM OLD.failure_code THEN
            RAISE EXCEPTION 'a settled attempt keeps its outcome' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.state IN ('REJECTED', 'ABSENT') AND NEW.filled_qty <> OLD.filled_qty THEN
            RAISE EXCEPTION 'a rejected or absent attempt has no fills' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.state = 'AUTHORIZED' AND NEW.state IN ('SUBMITTING', 'REJECTED'))
        OR (OLD.state = 'SUBMITTING' AND NEW.state IN ('WORKING', 'REJECTED', 'UNKNOWN'))
        OR (OLD.state = 'WORKING' AND NEW.state IN ('CANCEL_REQUESTED', 'FILLED', 'CANCELLED', 'EXPIRED', 'UNKNOWN'))
        OR (OLD.state = 'CANCEL_REQUESTED' AND NEW.state IN ('CANCELLED', 'FILLED', 'WORKING', 'EXPIRED', 'UNKNOWN'))
        OR (OLD.state = 'UNKNOWN' AND NEW.state IN ('WORKING', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED', 'ABSENT'))
    ) THEN
        RAISE EXCEPTION 'attempt transition % -> % is not allowed', OLD.state, NEW.state USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'SUBMITTING' AND NEW.submitting_at IS NULL THEN
        RAISE EXCEPTION 'the submit mark is committed before any I/O' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'SUBMITTING' THEN
        PERFORM td_check_time(NEW.submitting_at);
    END IF;
    IF OLD.state = 'AUTHORIZED' AND NEW.state = 'REJECTED' AND NEW.failure_code <> 'NOT_SENT' THEN
        RAISE EXCEPTION 'an attempt that was never sent is closed as NOT_SENT' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'ABSENT' THEN
        -- absence proof (conservative, unverified against a real exchange): two OK reconciliations OF THIS
        -- VENUE that STARTED at least 120 s after the submit mark and at least 60 s apart, and no run
        -- ever named this client id
        SELECT venue INTO venue_ FROM order_intents WHERE id = OLD.intent_id;
        SELECT count(*), max(r.started_at) - min(r.started_at) INTO proofs, spread
            FROM reconciliation_runs r
            WHERE r.venue = venue_ AND r.outcome = 'OK'
              AND r.started_at >= OLD.submitting_at + interval '120 seconds'
              AND r.finished_at <= p_now;
        IF proofs < 2 OR spread < interval '60 seconds' THEN
            RAISE EXCEPTION 'absence needs two successful reconciliations, 60 seconds apart, after the wait window' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM reconciliation_findings f WHERE f.subject = OLD.client_order_id::text) THEN
            RAISE EXCEPTION 'an order with this client id was seen; absence is not proven' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;

DROP TABLE IF EXISTS live_arming;
DROP TABLE IF EXISTS live_attestations;
DROP FUNCTION IF EXISTS live_arming_guard();
DROP FUNCTION IF EXISTS live_attestation_guard();
DROP FUNCTION IF EXISTS td_live_armed(timestamptz);
ALTER TABLE order_intents DROP CONSTRAINT order_intents_live_cap;
ALTER TABLE reconciliation_runs DROP CONSTRAINT reconciliation_runs_venue_check;
ALTER TABLE reconciliation_runs ADD CONSTRAINT reconciliation_runs_venue_check CHECK (venue IN ('PAPER', 'FAKE'));
ALTER TABLE venue_baselines DROP CONSTRAINT venue_baselines_venue_check;
ALTER TABLE venue_baselines ADD CONSTRAINT venue_baselines_venue_check CHECK (venue IN ('PAPER', 'FAKE'));
ALTER TABLE order_intents DROP CONSTRAINT order_intents_venue_check;
ALTER TABLE order_intents ADD CONSTRAINT order_intents_venue_check CHECK (venue IN ('PAPER', 'FAKE'));
ALTER TABLE attempt_fills DROP CONSTRAINT attempt_fills_venue_check;
ALTER TABLE attempt_fills ADD CONSTRAINT attempt_fills_venue_check CHECK (venue IN ('PAPER', 'FAKE'));
ALTER TABLE order_hints DROP CONSTRAINT order_hints_venue_check;
ALTER TABLE order_hints ADD CONSTRAINT order_hints_venue_check CHECK (venue IN ('PAPER', 'FAKE'));
ALTER TABLE api_events DROP CONSTRAINT api_events_venue_check;
ALTER TABLE api_events ADD CONSTRAINT api_events_venue_check CHECK (venue IN ('PAPER', 'FAKE', 'LIVE_READ'));
