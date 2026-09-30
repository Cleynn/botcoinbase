-- Phase 9 review fixes. (1) The paper profile changes only while the paper session is PAUSED, with no
-- open paper orders and with existing state inside the new limits; a cancel is never blocked by the
-- capital check. (2) Order authorization is serialized: attempts take the control row FOR SHARE and
-- run the money rules under one advisory lock.

CREATE OR REPLACE FUNCTION bot_control_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    actor text := td_actor_class();
    is_pause boolean; is_resume boolean; kill_on boolean; kill_off boolean;
    tripped boolean; brk_close boolean; rec_reset boolean; rec_done boolean;
    unknowns integer;
    prof_change boolean;
    np capital_profiles%ROWTYPE;
    pcash numeric;
    pcost numeric;
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
    prof_change := NEW.paper_profile IS DISTINCT FROM OLD.paper_profile
                   OR NEW.live_profile IS DISTINCT FROM OLD.live_profile;

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
            OR (NEW.boot_id IS DISTINCT FROM OLD.boot_id) OR prof_change) THEN
        RAISE EXCEPTION 'bot control update changes nothing recognised' USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- a capital profile is chosen by the ADMIN dashboard only, while the bot is PAUSED, and never
    -- together with another control change
    IF prof_change THEN
        IF actor <> 'WEB' THEN
            RAISE EXCEPTION 'only the ADMIN dashboard selects a capital profile' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.bot_state <> 'PAUSED' OR NEW.bot_state <> 'PAUSED' THEN
            RAISE EXCEPTION 'a capital profile changes only while the bot is PAUSED' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF is_pause OR is_resume OR kill_on OR kill_off OR tripped OR brk_close OR rec_reset OR rec_done
           OR NEW.boot_id IS DISTINCT FROM OLD.boot_id THEN
            RAISE EXCEPTION 'a capital profile change is never combined with another control change' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.paper_profile IS DISTINCT FROM OLD.paper_profile AND NEW.live_profile IS DISTINCT FROM OLD.live_profile THEN
            RAISE EXCEPTION 'one profile changes at a time' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        -- the paper trader does not follow bot_state, so the paper side must be idle and within the new limits
        IF NEW.paper_profile IS DISTINCT FROM OLD.paper_profile THEN
            SELECT * INTO np FROM capital_profiles WHERE name = NEW.paper_profile;
            IF COALESCE((SELECT state FROM paper_session WHERE id), 'RUNNING') <> 'PAUSED' THEN
                RAISE EXCEPTION 'the paper session must be PAUSED to change the paper profile' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF EXISTS (SELECT 1 FROM paper_orders WHERE state = 'OPEN') THEN
                RAISE EXCEPTION 'open paper orders must be cancelled before the paper profile changes' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT COALESCE(sum(quote_delta), 0) INTO pcash FROM paper_ledger_entries;
            SELECT COALESCE(sum(cost_basis), 0) INTO pcost FROM paper_positions;
            IF pcash > 0 AND (pcash < np.protected_reserve OR pcost > np.max_deployment) THEN
                RAISE EXCEPTION 'existing paper state exceeds the limits of the new profile' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
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

CREATE OR REPLACE FUNCTION paper_capital_check() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    cash numeric;
    reserved numeric;
    cost numeric;
    prof capital_profiles%ROWTYPE;
BEGIN
    -- a cancel (or fill) closing an OPEN order only releases reserve: it is never blocked, so the
    -- safety cancel cannot be wedged by a limit that is already exceeded
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'paper_orders' THEN
        IF OLD.state = 'OPEN' AND NEW.state <> 'OPEN' AND NEW.quote_reserved <= OLD.quote_reserved THEN
            RETURN NULL;
        END IF;
    END IF;
    prof := td_capital_profile('PAPER');
    IF prof.name IS NULL THEN
        RAISE EXCEPTION 'no capital profile is selected' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE(sum(quote_delta), 0) INTO cash FROM paper_ledger_entries;
    SELECT COALESCE(sum(quote_reserved), 0) INTO reserved FROM paper_orders WHERE state = 'OPEN';
    SELECT COALESCE(sum(cost_basis), 0) INTO cost FROM paper_positions;
    IF cash > 0 AND cash - reserved < prof.protected_reserve THEN
        RAISE EXCEPTION 'paper reserve breached: free cash below the profile reserve' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF reserved + cost > prof.max_deployment THEN
        RAISE EXCEPTION 'paper deployment cap breached: above the profile cap' USING ERRCODE = 'integrity_constraint_violation';
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
