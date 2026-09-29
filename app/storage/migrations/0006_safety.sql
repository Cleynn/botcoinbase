-- Phase 8: safety machinery. Bot control (kill switch, breaker, recovery), order intents and
-- attempts, pre-trade decisions, reconciliation records, supplemental order hints, control commands.
--
-- Write model:
--   td_app (web)  PAUSE, ACTIVATE KILL, RESUME (only when every guard below holds), and enqueue a
--                 CANCEL_KNOWN command. It can NEVER insert an intent, decision, attempt, fill or
--                 reconciliation row: the dashboard cannot create an order.
--   td_ctl (host) writes intents, decisions, attempts, reconciliations, hints, fills, opens the
--                 breaker, marks recovery, releases the kill switch (host only) and runs commands.
-- Times inside guards are the row's own supplied time (the application clock), not the database
-- clock (documented deviation from BI-36, see DEC-021). LIVE is unrepresentable: venue CHECKs list
-- PAPER and FAKE only, and there is no live gateway in this build.

-- ------------------------------------------------------------------ bot control (singleton)
CREATE TABLE bot_control (
    id                    boolean     PRIMARY KEY DEFAULT true CHECK (id),
    bot_state             text        NOT NULL DEFAULT 'PAUSED' CHECK (bot_state IN ('PAUSED', 'RUNNING')),
    kill_switch           text        NOT NULL DEFAULT 'INACTIVE' CHECK (kill_switch IN ('INACTIVE', 'ACTIVE')),
    kill_reason           text        CHECK (kill_reason ~ '^[A-Z_]{3,40}$'),
    kill_activated_at     timestamptz,
    breaker_state         text        NOT NULL DEFAULT 'CLOSED' CHECK (breaker_state IN ('CLOSED', 'OPEN')),
    breaker_reason        text        CHECK (breaker_reason ~ '^[A-Z_]{3,40}$'),
    breaker_opened_at     timestamptz,
    breaker_cooldown_until timestamptz,
    recovery_state        text        NOT NULL DEFAULT 'INCOMPLETE' CHECK (recovery_state IN ('INCOMPLETE', 'COMPLETE')),
    recovery_completed_at timestamptz,
    boot_id               text        CHECK (boot_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    last_change_reason    text        NOT NULL DEFAULT 'INITIAL' CHECK (last_change_reason ~ '^[A-Z_]{3,40}$'),
    version               integer     NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at            timestamptz NOT NULL,
    CONSTRAINT bot_kill_has_reason CHECK (kill_switch = 'INACTIVE' OR (kill_reason IS NOT NULL AND kill_activated_at IS NOT NULL)),
    CONSTRAINT bot_breaker_has_reason CHECK (breaker_state = 'CLOSED' OR (breaker_reason IS NOT NULL AND breaker_opened_at IS NOT NULL AND breaker_cooldown_until IS NOT NULL)),
    -- The bot can be RUNNING only when nothing forbids it. This is the state-level backstop.
    CONSTRAINT bot_running_is_safe CHECK (
        bot_state <> 'RUNNING' OR (kill_switch = 'INACTIVE' AND breaker_state = 'CLOSED' AND recovery_state = 'COMPLETE'))
);
INSERT INTO bot_control (id, updated_at) VALUES (true, now());

CREATE TABLE bot_control_history (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at    timestamptz NOT NULL,
    actor_class    text        NOT NULL CHECK (actor_class IN ('WEB', 'HOST')),
    event          text        NOT NULL CHECK (event IN ('PAUSE', 'KILL_ACTIVATE', 'KILL_RELEASE', 'BREAKER_OPEN', 'RESUME', 'RECOVERY_INCOMPLETE', 'RECOVERY_COMPLETE')),
    bot_state      text        NOT NULL,
    kill_switch    text        NOT NULL,
    breaker_state  text        NOT NULL,
    recovery_state text        NOT NULL,
    reason         text        NOT NULL CHECK (reason ~ '^[A-Z_]{3,40}$'),
    version        integer     NOT NULL
);

-- ------------------------------------------------------------------ reconciliation (host writes)
CREATE TABLE reconciliation_runs (
    id            uuid        PRIMARY KEY,
    venue         text        NOT NULL CHECK (venue IN ('PAPER', 'FAKE')),
    trigger       text        NOT NULL CHECK (trigger IN ('STARTUP', 'SCHEDULED', 'MANUAL', 'PRE_RESUME', 'AFTER_AMBIGUITY', 'WEBSOCKET')),
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL,
    outcome       text        NOT NULL CHECK (outcome IN ('OK', 'MISMATCH', 'FAILED')),
    orders_seen   integer     NOT NULL DEFAULT 0 CHECK (orders_seen >= 0),
    fills_seen    integer     NOT NULL DEFAULT 0 CHECK (fills_seen >= 0),
    balances_seen integer     NOT NULL DEFAULT 0 CHECK (balances_seen >= 0),
    findings_count integer    NOT NULL DEFAULT 0 CHECK (findings_count >= 0),
    failure_code  text        CHECK (failure_code ~ '^[A-Z_]{3,40}$'),
    CONSTRAINT recon_times CHECK (finished_at >= started_at),
    CONSTRAINT recon_ok_is_clean CHECK (outcome <> 'OK' OR (findings_count = 0 AND failure_code IS NULL)),
    CONSTRAINT recon_failed_has_code CHECK (outcome <> 'FAILED' OR failure_code IS NOT NULL),
    CONSTRAINT recon_mismatch_has_findings CHECK (outcome <> 'MISMATCH' OR findings_count > 0)
);
CREATE INDEX reconciliation_runs_recent_idx ON reconciliation_runs (finished_at DESC);

CREATE TABLE reconciliation_findings (
    id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id    uuid        NOT NULL REFERENCES reconciliation_runs (id) ON DELETE RESTRICT,
    code      text        NOT NULL CHECK (code IN (
        'UNKNOWN_ORDER', 'MISSING_ORDER', 'STATUS_MISMATCH', 'FILL_MISMATCH', 'UNKNOWN_FILL',
        'BALANCE_MISMATCH', 'DUPLICATE_CLIENT_ID', 'FILL_ANOMALY', 'ORDER_MISMATCH',
        'WS_REST_CONFLICT', 'UNKNOWN_STATUS', 'ORDER_APPEARED_AFTER_ABSENCE', 'UNKNOWN_ATTEMPT')),
    subject   text        CHECK (subject ~ '^[A-Za-z0-9._:-]{1,64}$'),
    detail    text        CHECK (char_length(detail) <= 200 AND detail !~ '[[:cntrl:]]')
);
CREATE INDEX reconciliation_findings_run_idx ON reconciliation_findings (run_id);
CREATE INDEX reconciliation_findings_subject_idx ON reconciliation_findings (subject);

-- ------------------------------------------------------------------ baselines and intents
CREATE TABLE venue_baselines (
    venue       text        NOT NULL CHECK (venue IN ('PAPER', 'FAKE')),
    currency    text        NOT NULL CHECK (currency ~ '^[A-Z0-9]{1,20}$'),
    amount      numeric     NOT NULL CHECK (amount >= 0 AND amount < 1e18),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (venue, currency)
);

CREATE TABLE order_intents (
    id                    uuid        PRIMARY KEY,
    intent_key            text        NOT NULL CHECK (intent_key ~ '^[0-9a-f]{64}$'),
    venue                 text        NOT NULL CHECK (venue IN ('PAPER', 'FAKE')),
    pair_id               uuid        NOT NULL REFERENCES pairs (id) ON DELETE RESTRICT,
    product_id            text        NOT NULL CHECK (product_id ~ '^[A-Z0-9]+-USDC$'),
    side                  text        NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type            text        NOT NULL CHECK (order_type = 'limit_limit_gtc'),
    post_only             boolean     NOT NULL CHECK (post_only),
    price                 numeric     NOT NULL CHECK (price > 0 AND price < 1e18),
    base_qty              numeric     NOT NULL CHECK (base_qty > 0 AND base_qty < 1e18),
    expected_cycle_return numeric     CHECK (expected_cycle_return IS NULL OR (expected_cycle_return > -1 AND expected_cycle_return < 1)),
    source                text        NOT NULL CHECK (source ~ '^[a-z_]{3,32}$'),
    created_at            timestamptz NOT NULL,
    CONSTRAINT order_intents_key_unique UNIQUE (intent_key),
    -- the per-order cap is a hard ceiling in the schema too (constants.POLICY_MAX_ORDER_NOTIONAL)
    CONSTRAINT order_intents_cap CHECK (price * base_qty <= 12)
);
CREATE INDEX order_intents_recent_idx ON order_intents (created_at DESC);

CREATE TABLE risk_decisions (
    id          uuid        PRIMARY KEY,
    intent_id   uuid        NOT NULL REFERENCES order_intents (id) ON DELETE RESTRICT,
    decision    text        NOT NULL CHECK (decision IN ('ALLOW', 'BLOCK')),
    reasons     text[]      NOT NULL CHECK (cardinality(reasons) <= 40),
    inputs_hash text        NOT NULL CHECK (inputs_hash ~ '^[0-9a-f]{64}$'),
    decided_at  timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    consumed_at timestamptz,
    CONSTRAINT decision_reasons_match CHECK ((decision = 'ALLOW') = (cardinality(reasons) = 0)),
    CONSTRAINT decision_ttl CHECK (expires_at > decided_at AND expires_at <= decided_at + interval '30 seconds'),
    CONSTRAINT decision_only_allow_consumed CHECK (consumed_at IS NULL OR decision = 'ALLOW')
);
CREATE INDEX risk_decisions_intent_idx ON risk_decisions (intent_id, decided_at DESC);

CREATE TABLE order_attempts (
    id                uuid        PRIMARY KEY,
    intent_id         uuid        NOT NULL REFERENCES order_intents (id) ON DELETE RESTRICT,
    attempt_no        integer     NOT NULL CHECK (attempt_no BETWEEN 1 AND 5),
    client_order_id   uuid        NOT NULL,
    decision_id       uuid        NOT NULL REFERENCES risk_decisions (id) ON DELETE RESTRICT,
    boot_id           text        NOT NULL CHECK (boot_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    state             text        NOT NULL CHECK (state IN (
        'AUTHORIZED', 'SUBMITTING', 'WORKING', 'CANCEL_REQUESTED', 'FILLED', 'CANCELLED',
        'EXPIRED', 'REJECTED', 'UNKNOWN', 'ABSENT')),
    exchange_order_id text        CHECK (exchange_order_id ~ '^[A-Za-z0-9-]{8,64}$'),
    filled_qty        numeric     NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
    failure_code      text        CHECK (failure_code ~ '^[A-Z_]{3,40}$'),
    created_at        timestamptz NOT NULL,
    submitting_at     timestamptz,
    updated_at        timestamptz NOT NULL,
    CONSTRAINT order_attempts_client_unique UNIQUE (client_order_id),
    CONSTRAINT order_attempts_decision_unique UNIQUE (decision_id),
    CONSTRAINT order_attempts_number_unique UNIQUE (intent_id, attempt_no),
    CONSTRAINT order_attempts_submitted_marked CHECK (state = 'AUTHORIZED' OR submitting_at IS NOT NULL OR failure_code = 'NOT_SENT'),
    CONSTRAINT order_attempts_rejected_has_code CHECK (state <> 'REJECTED' OR failure_code IS NOT NULL)
);
-- At most one live (non-terminal) attempt per intent.
CREATE UNIQUE INDEX order_attempts_one_live ON order_attempts (intent_id)
    WHERE state IN ('AUTHORIZED', 'SUBMITTING', 'WORKING', 'CANCEL_REQUESTED', 'UNKNOWN');
CREATE INDEX order_attempts_open_idx ON order_attempts (state)
    WHERE state IN ('AUTHORIZED', 'SUBMITTING', 'WORKING', 'CANCEL_REQUESTED', 'UNKNOWN');

CREATE TABLE attempt_fills (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id       uuid        NOT NULL REFERENCES order_attempts (id) ON DELETE RESTRICT,
    venue            text        NOT NULL CHECK (venue IN ('PAPER', 'FAKE')),
    exchange_fill_id text        NOT NULL CHECK (exchange_fill_id ~ '^[A-Za-z0-9-]{8,64}$'),
    side             text        NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price            numeric     NOT NULL CHECK (price > 0),
    size             numeric     NOT NULL CHECK (size > 0),
    fee              numeric     NOT NULL CHECK (fee >= 0),
    liquidity        text        NOT NULL CHECK (liquidity IN ('MAKER', 'TAKER', 'UNKNOWN')),
    occurred_at      timestamptz NOT NULL,
    CONSTRAINT attempt_fills_unique UNIQUE (venue, exchange_fill_id)
);

CREATE TABLE order_hints (
    id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue           text        NOT NULL CHECK (venue IN ('PAPER', 'FAKE')),
    client_order_id text        NOT NULL CHECK (client_order_id ~ '^[A-Za-z0-9-]{8,64}$'),
    order_id        text        NOT NULL CHECK (order_id ~ '^[A-Za-z0-9-]{8,64}$'),
    status          text        NOT NULL CHECK (status IN ('WORKING', 'CANCEL_REQUESTED', 'FILLED', 'CANCELLED', 'EXPIRED', 'FAILED', 'UNKNOWN')),
    filled_qty      numeric     NOT NULL CHECK (filled_qty >= 0),
    sequence        integer     NOT NULL CHECK (sequence >= 0),
    received_at     timestamptz NOT NULL
);
CREATE INDEX order_hints_client_idx ON order_hints (client_order_id, received_at DESC);

CREATE TABLE api_events (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue       text        NOT NULL CHECK (venue IN ('PAPER', 'FAKE', 'LIVE_READ')),
    operation   text        NOT NULL CHECK (operation IN ('list_accounts', 'list_orders', 'list_fills', 'get_order', 'key_permissions', 'submit', 'cancel')),
    ok          boolean     NOT NULL,
    code        text        CHECK (code ~ '^[A-Z_]{3,40}$'),
    occurred_at timestamptz NOT NULL,
    CONSTRAINT api_events_failed_has_code CHECK (ok OR code IS NOT NULL)
);
CREATE INDEX api_events_recent_idx ON api_events (occurred_at DESC);

CREATE TABLE control_commands (
    id           uuid        PRIMARY KEY,
    kind         text        NOT NULL CHECK (kind = 'CANCEL_KNOWN'),
    origin       text        NOT NULL CHECK (origin IN ('OPERATOR', 'KILL_SWITCH')),
    state        text        NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING', 'RUNNING', 'DONE', 'FAILED')),
    requested_by uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    requested_at timestamptz NOT NULL,
    finished_at  timestamptz,
    result       jsonb       CHECK (result IS NULL OR octet_length(result::text) <= 4096),
    failure_code text        CHECK (failure_code ~ '^[A-Z_]{3,40}$'),
    CONSTRAINT command_finished_consistent CHECK ((state IN ('DONE', 'FAILED')) = (finished_at IS NOT NULL)),
    CONSTRAINT command_failed_has_code CHECK (state <> 'FAILED' OR failure_code IS NOT NULL)
);
CREATE UNIQUE INDEX control_commands_single_flight ON control_commands ((true)) WHERE state IN ('PENDING', 'RUNNING');

-- ================================================================== guards
CREATE FUNCTION td_fresh_reconciliation(p_now timestamptz, p_after timestamptz) RETURNS boolean
    LANGUAGE sql STABLE SET search_path = pg_catalog, public AS
$fn$
    -- The newest run overall must be OK, finished within 300 s before p_now, and (when p_after is
    -- given) finished after it. A newer failed run cancels an older good one.
    SELECT COALESCE((
        SELECT outcome = 'OK' AND finished_at <= p_now AND finished_at >= p_now - interval '300 seconds'
               AND (p_after IS NULL OR finished_at > p_after)
        FROM reconciliation_runs ORDER BY finished_at DESC, started_at DESC LIMIT 1), false)
$fn$;

CREATE FUNCTION bot_control_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    actor text := td_actor_class();
    is_pause boolean; is_resume boolean; kill_on boolean; kill_off boolean;
    tripped boolean; brk_close boolean; rec_reset boolean; rec_done boolean;
    unknowns integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bot control is never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF actor IS NULL THEN
        RAISE EXCEPTION 'unknown database role for bot control' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
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
        IF NOT td_fresh_reconciliation(NEW.updated_at, NULL) THEN
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
        IF OLD.breaker_state = 'OPEN' AND OLD.breaker_cooldown_until > NEW.updated_at THEN
            RAISE EXCEPTION 'the breaker cooldown has not elapsed' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT td_fresh_reconciliation(NEW.updated_at, CASE WHEN OLD.breaker_state = 'OPEN' THEN OLD.breaker_opened_at END) THEN
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
CREATE TRIGGER bot_control_guard_trigger BEFORE UPDATE OR DELETE ON bot_control
    FOR EACH ROW EXECUTE FUNCTION bot_control_guard();
CREATE TRIGGER bot_control_no_truncate BEFORE TRUNCATE ON bot_control
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

CREATE FUNCTION bot_control_record() RETURNS trigger
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
CREATE TRIGGER bot_control_record_trigger AFTER UPDATE ON bot_control
    FOR EACH ROW EXECUTE FUNCTION bot_control_record();

-- history rows come only from the record trigger above
CREATE FUNCTION bot_control_history_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'bot control history is written by the control trigger only' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER bot_control_history_guard_trigger BEFORE INSERT ON bot_control_history
    FOR EACH ROW EXECUTE FUNCTION bot_control_history_guard();

-- ------------------------------------------------------------------ host-only inserts
CREATE FUNCTION td_host_only() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF td_actor_class() IS DISTINCT FROM 'HOST' THEN
        RAISE EXCEPTION '% is written by the host only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION risk_decision_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'risk decisions are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS DISTINCT FROM 'HOST' THEN
        RAISE EXCEPTION 'risk decisions are written by the host only' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.consumed_at IS NOT NULL THEN
            RAISE EXCEPTION 'a new decision is not consumed' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.intent_id <> OLD.intent_id OR NEW.decision <> OLD.decision OR NEW.reasons <> OLD.reasons
       OR NEW.inputs_hash <> OLD.inputs_hash OR NEW.decided_at <> OLD.decided_at OR NEW.expires_at <> OLD.expires_at THEN
        RAISE EXCEPTION 'risk decisions are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL THEN
        RAISE EXCEPTION 'one ALLOW authorizes one attempt' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER risk_decisions_guard BEFORE INSERT OR UPDATE OR DELETE ON risk_decisions
    FOR EACH ROW EXECUTE FUNCTION risk_decision_guard();
CREATE TRIGGER risk_decisions_no_truncate BEFORE TRUNCATE ON risk_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- An attempt is authorized only here: the hot guards are read from base tables at this moment,
-- the decision is checked and consumed, and the client id must not be reused.
CREATE FUNCTION order_attempt_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    ctl bot_control%ROWTYPE;
    dec risk_decisions%ROWTYPE;
    prev integer;
    bad integer;
    proofs integer;
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
        SELECT * INTO ctl FROM bot_control WHERE id;
        IF ctl.kill_switch <> 'INACTIVE' OR ctl.breaker_state <> 'CLOSED' OR ctl.bot_state <> 'RUNNING'
           OR ctl.recovery_state <> 'COMPLETE' THEN
            RAISE EXCEPTION 'orders are not authorized while the bot is not RUNNING and clear' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT td_fresh_reconciliation(NEW.created_at, NULL) THEN
            RAISE EXCEPTION 'orders need a current successful reconciliation' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM order_attempts WHERE state = 'UNKNOWN') THEN
            RAISE EXCEPTION 'orders are blocked while an attempt has an unknown outcome' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT * INTO dec FROM risk_decisions WHERE id = NEW.decision_id FOR UPDATE;
        IF NOT FOUND OR dec.decision <> 'ALLOW' OR dec.intent_id <> NEW.intent_id OR dec.consumed_at IS NOT NULL
           OR dec.expires_at < NEW.created_at OR dec.decided_at > NEW.created_at THEN
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
    IF OLD.state = 'AUTHORIZED' AND NEW.state = 'REJECTED' AND NEW.failure_code <> 'NOT_SENT' THEN
        RAISE EXCEPTION 'an attempt that was never sent is closed as NOT_SENT' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'ABSENT' THEN
        -- absence proof (conservative, unverified against a real exchange): two OK reconciliations
        -- that STARTED at least 120 s after the submit mark, and neither ever saw this client id
        SELECT count(*) INTO proofs FROM reconciliation_runs r
            WHERE r.outcome = 'OK' AND r.started_at >= OLD.submitting_at + interval '120 seconds'
              AND r.finished_at <= NEW.updated_at;
        IF proofs < 2 THEN
            RAISE EXCEPTION 'absence needs two successful reconciliations after the wait window' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM reconciliation_findings f WHERE f.subject = OLD.client_order_id::text) THEN
            RAISE EXCEPTION 'an order with this client id was seen; absence is not proven' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER order_attempts_guard BEFORE INSERT OR UPDATE OR DELETE ON order_attempts
    FOR EACH ROW EXECUTE FUNCTION order_attempt_guard();
CREATE TRIGGER order_attempts_no_truncate BEFORE TRUNCATE ON order_attempts
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- Intents, reconciliation records, fills, hints, baselines: host-written and immutable.
DO $do$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['order_intents', 'reconciliation_runs', 'reconciliation_findings',
                             'attempt_fills', 'order_hints', 'venue_baselines', 'bot_control_history']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION td_append_only()', t || '_append_only', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION td_append_only()', t || '_no_truncate', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['order_intents', 'reconciliation_runs', 'reconciliation_findings',
                             'attempt_fills', 'order_hints', 'venue_baselines', 'api_events']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION td_host_only()', t || '_host_only', t);
    END LOOP;
END
$do$;

-- api_events: host inserts; the host may prune rows older than seven days, nothing else changes
CREATE FUNCTION api_events_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP = 'DELETE' AND td_actor_class() = 'HOST' AND OLD.occurred_at < clock_timestamp() - interval '7 days' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'api events are append-only' USING ERRCODE = 'integrity_constraint_violation';
END
$fn$;
CREATE TRIGGER api_events_guard_trigger BEFORE UPDATE OR DELETE ON api_events
    FOR EACH ROW EXECUTE FUNCTION api_events_guard();
CREATE TRIGGER api_events_no_truncate BEFORE TRUNCATE ON api_events
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- control commands: the web enqueues, the host runs; a command finishes once
CREATE FUNCTION control_command_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE actor text := td_actor_class();
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'control commands are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF actor NOT IN ('WEB', 'HOST') OR NEW.state <> 'PENDING' OR NEW.result IS NOT NULL OR NEW.finished_at IS NOT NULL THEN
            RAISE EXCEPTION 'a command starts PENDING' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF actor <> 'HOST' THEN
        RAISE EXCEPTION 'only the host runs control commands' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.kind <> OLD.kind OR NEW.origin <> OLD.origin OR NEW.requested_by <> OLD.requested_by
       OR NEW.requested_at <> OLD.requested_at THEN
        RAISE EXCEPTION 'command request fields are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT ((OLD.state = 'PENDING' AND NEW.state IN ('RUNNING', 'DONE', 'FAILED'))
            OR (OLD.state = 'RUNNING' AND NEW.state IN ('DONE', 'FAILED'))) THEN
        RAISE EXCEPTION 'command transition % -> % is not allowed', OLD.state, NEW.state USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER control_commands_guard BEFORE INSERT OR UPDATE OR DELETE ON control_commands
    FOR EACH ROW EXECUTE FUNCTION control_command_guard();
CREATE TRIGGER control_commands_no_truncate BEFORE TRUNCATE ON control_commands
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- ------------------------------------------------------------------ grants
GRANT SELECT ON bot_control, bot_control_history, reconciliation_runs, reconciliation_findings,
    venue_baselines, order_intents, risk_decisions, order_attempts, attempt_fills, order_hints,
    api_events, control_commands TO td_app, td_ctl;
GRANT UPDATE (bot_state, kill_switch, kill_reason, kill_activated_at, breaker_state, breaker_reason,
    breaker_opened_at, breaker_cooldown_until, last_change_reason, version, updated_at) ON bot_control TO td_app;
GRANT UPDATE (bot_state, kill_switch, kill_reason, kill_activated_at, breaker_state, breaker_reason,
    breaker_opened_at, breaker_cooldown_until, recovery_state, recovery_completed_at, boot_id,
    last_change_reason, version, updated_at) ON bot_control TO td_ctl;
GRANT INSERT ON bot_control_history TO td_app, td_ctl;
GRANT INSERT ON control_commands TO td_app, td_ctl;
GRANT UPDATE (state, finished_at, result, failure_code) ON control_commands TO td_ctl;
GRANT INSERT ON reconciliation_runs, reconciliation_findings, venue_baselines, order_intents,
    risk_decisions, order_attempts, attempt_fills, order_hints, api_events TO td_ctl;
GRANT UPDATE (consumed_at) ON risk_decisions TO td_ctl;
GRANT UPDATE (state, exchange_order_id, filled_qty, failure_code, submitting_at, updated_at) ON order_attempts TO td_ctl;
GRANT DELETE ON api_events TO td_ctl;
