-- Phase 10: live trading plumbing on the COINBASE venue.
-- * COINBASE becomes a representable venue. NOTHING can be ordered on it unless the host has armed it:
--   the attempt guard refuses a COINBASE attempt without an active row in live_arming that was made
--   after the last kill switch, breaker trip, recovery reset or LIVE profile change.
-- * live_attestations: human attestations (independent review, exchange contract, signed pilot review,
--   soak, DEC-000, tradability, key scope). Host-only, append-only. They are CLAIMS by a person, not
--   facts the code can verify.
-- * live_arming: a time-boxed (<= 24 h) arming made on the host. The web role cannot write either table.
-- * A table CHECK caps every COINBASE intent at 12 USDC whatever profile is selected.
ALTER TABLE reconciliation_runs DROP CONSTRAINT reconciliation_runs_venue_check;
ALTER TABLE reconciliation_runs ADD CONSTRAINT reconciliation_runs_venue_check CHECK (venue IN ('PAPER', 'FAKE', 'COINBASE'));
ALTER TABLE venue_baselines DROP CONSTRAINT venue_baselines_venue_check;
ALTER TABLE venue_baselines ADD CONSTRAINT venue_baselines_venue_check CHECK (venue IN ('PAPER', 'FAKE', 'COINBASE'));
ALTER TABLE order_intents DROP CONSTRAINT order_intents_venue_check;
ALTER TABLE order_intents ADD CONSTRAINT order_intents_venue_check CHECK (venue IN ('PAPER', 'FAKE', 'COINBASE'));
ALTER TABLE attempt_fills DROP CONSTRAINT attempt_fills_venue_check;
ALTER TABLE attempt_fills ADD CONSTRAINT attempt_fills_venue_check CHECK (venue IN ('PAPER', 'FAKE', 'COINBASE'));
ALTER TABLE order_hints DROP CONSTRAINT order_hints_venue_check;
ALTER TABLE order_hints ADD CONSTRAINT order_hints_venue_check CHECK (venue IN ('PAPER', 'FAKE', 'COINBASE'));
ALTER TABLE api_events DROP CONSTRAINT api_events_venue_check;
ALTER TABLE api_events ADD CONSTRAINT api_events_venue_check CHECK (venue IN ('PAPER', 'FAKE', 'LIVE_READ', 'COINBASE'));

ALTER TABLE order_intents ADD CONSTRAINT order_intents_live_cap
    CHECK (venue <> 'COINBASE' OR price * base_qty <= 12);

CREATE TABLE live_attestations (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        text        NOT NULL CHECK (code IN ('INDEPENDENT_REVIEW_DONE', 'EXCHANGE_CONTRACT_REVIEWED', 'SIGNED_PILOT_REVIEW', 'SOAK_COMPLETE', 'DEC000_ACKNOWLEDGED', 'TRADABILITY_CONFIRMED', 'KEY_SCOPE_CONFIRMED')),
    attested_at timestamptz NOT NULL,
    attested_by text        NOT NULL CHECK (attested_by ~ '^[a-z0-9_.-]{1,40}$'),
    note        text        NOT NULL CHECK (char_length(note) BETWEEN 10 AND 300)
);
CREATE INDEX live_attestations_code_idx ON live_attestations (code, attested_at DESC);

CREATE FUNCTION live_attestation_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF td_actor_class() IS DISTINCT FROM 'HOST' THEN
        RAISE EXCEPTION 'live attestations are written by the host only' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    PERFORM td_check_time(NEW.attested_at);
    RETURN NEW;
END
$fn$;
CREATE TRIGGER live_attestations_guard BEFORE INSERT ON live_attestations
    FOR EACH ROW EXECUTE FUNCTION live_attestation_guard();
CREATE TRIGGER live_attestations_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON live_attestations
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

CREATE TABLE live_arming (
    id         uuid        PRIMARY KEY,
    armed_at   timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    armed_by   text        NOT NULL CHECK (armed_by ~ '^[a-z0-9_.-]{1,40}$'),
    key_hint   text        NOT NULL CHECK (key_hint ~ '^[.]{3}.{6}$'),
    checks     jsonb       NOT NULL CHECK (octet_length(checks::text) <= 2000),
    revoked_at timestamptz,
    CONSTRAINT live_arming_window CHECK (expires_at > armed_at AND expires_at <= armed_at + interval '24 hours'),
    CONSTRAINT live_arming_revoke CHECK (revoked_at IS NULL OR revoked_at >= armed_at)
);

-- Is there an arming that is unrevoked, unexpired, and not undone by a later kill switch, breaker
-- trip, recovery reset or change of the LIVE capital profile?
CREATE FUNCTION td_live_armed(p_now timestamptz) RETURNS boolean
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

CREATE FUNCTION live_arming_guard() RETURNS trigger
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
CREATE TRIGGER live_arming_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON live_arming
    FOR EACH ROW EXECUTE FUNCTION live_arming_guard();
CREATE TRIGGER live_arming_no_truncate BEFORE TRUNCATE ON live_arming
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

GRANT SELECT ON live_attestations, live_arming TO td_app, td_ctl;
GRANT INSERT ON live_attestations, live_arming TO td_ctl;
GRANT UPDATE (revoked_at) ON live_arming TO td_ctl;

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
    prof := td_capital_profile(CASE WHEN i.venue = 'COINBASE' THEN 'LIVE' ELSE 'PAPER' END);
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
        IF venue_ = 'COINBASE' AND NOT td_live_armed(p_now) THEN
            RAISE EXCEPTION 'live orders need an active arming made on the host since the last kill switch, breaker trip, recovery reset or LIVE profile change' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
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
