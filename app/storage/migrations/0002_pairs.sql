-- Phase 4: product discovery, pair candidates, validation evidence, lifecycle history.
-- Applied by `python -m app.storage.database migrate` as the database owner.
--
-- Write model (least privilege):
--   td_app (web)   proposes pairs and performs ADMIN lifecycle actions; cannot write products,
--                  metadata or validation evidence.
--   td_ctl (host)  discovery and validation runner (the baseline's td_worker duties, deferred);
--                  the only role that can write products, metadata snapshots and validation runs.
-- Every change to `pairs` is one transition: a trigger refuses anything not listed in
-- `allowed_transitions` for the caller's actor class, forbids DELETE, keeps identifying columns
-- immutable and requires a matching history row in the same transaction.

CREATE FUNCTION td_append_only() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS
$fn$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
END
$fn$;

CREATE FUNCTION td_actor_class() RETURNS text
    LANGUAGE sql STABLE SET search_path = pg_catalog AS
$fn$
    SELECT CASE current_user WHEN 'td_app' THEN 'WEB' WHEN 'td_ctl' THEN 'HOST' END
$fn$;

-- ------------------------------------------------------------------ products and metadata
CREATE TABLE products (
    id              uuid        PRIMARY KEY,
    product_id      text        NOT NULL
        CHECK (product_id ~ '^[A-Z0-9]+-USDC$' AND char_length(product_id) <= 24),
    base_currency   text        NOT NULL CHECK (base_currency ~ '^[A-Z0-9]{1,20}$'),
    quote_currency  text        NOT NULL CHECK (quote_currency = 'USDC'),
    product_type    text        NOT NULL CHECK (product_type = 'SPOT'),
    venue           text        NOT NULL CHECK (venue = 'CBE'),
    discovered_rank integer     CHECK (discovered_rank BETWEEN 1 AND 500),
    first_seen_at   timestamptz NOT NULL,
    last_seen_at    timestamptz NOT NULL,
    CONSTRAINT products_product_id_unique UNIQUE (product_id),
    CONSTRAINT products_id_consistent CHECK (product_id = base_currency || '-' || quote_currency)
);
CREATE INDEX products_rank_idx ON products (discovered_rank NULLS LAST, product_id);

-- Static rules only: no price, volume or spread. A new row exists only when the content changed.
CREATE TABLE product_metadata_snapshots (
    id               uuid        PRIMARY KEY,
    product_uuid     uuid        NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    sha256           text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status           text        NOT NULL
        CHECK (char_length(status) BETWEEN 1 AND 64 AND status !~ '[[:cntrl:]]'),
    is_disabled      boolean,
    trading_disabled boolean,
    cancel_only      boolean,
    limit_only       boolean,
    post_only        boolean,
    auction_mode     boolean,
    base_increment   numeric CHECK (base_increment IS NULL OR (base_increment > 0 AND base_increment < 1e18)),
    quote_increment  numeric CHECK (quote_increment IS NULL OR (quote_increment > 0 AND quote_increment < 1e18)),
    price_increment  numeric CHECK (price_increment IS NULL OR (price_increment > 0 AND price_increment < 1e18)),
    base_min_size    numeric CHECK (base_min_size IS NULL OR (base_min_size > 0 AND base_min_size < 1e18)),
    base_max_size    numeric CHECK (base_max_size IS NULL OR (base_max_size > 0 AND base_max_size < 1e30)),
    quote_min_size   numeric CHECK (quote_min_size IS NULL OR (quote_min_size > 0 AND quote_min_size < 1e18)),
    quote_max_size   numeric CHECK (quote_max_size IS NULL OR (quote_max_size > 0 AND quote_max_size < 1e30)),
    alias            text        CHECK (alias IS NULL OR alias ~ '^[A-Z0-9]+-[A-Z0-9]+$'),
    alias_to         text[]      NOT NULL DEFAULT '{}' CHECK (cardinality(alias_to) <= 10),
    malformed        text[]      NOT NULL DEFAULT '{}' CHECK (cardinality(malformed) <= 32),
    first_seen_at    timestamptz NOT NULL,
    CONSTRAINT product_metadata_snapshots_unique UNIQUE (product_uuid, sha256)
);
CREATE TRIGGER product_metadata_snapshots_append_only
    BEFORE UPDATE OR DELETE ON product_metadata_snapshots
    FOR EACH ROW EXECUTE FUNCTION td_append_only();
CREATE TRIGGER product_metadata_snapshots_no_truncate
    BEFORE TRUNCATE ON product_metadata_snapshots
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

CREATE TABLE product_metadata_current (
    product_uuid          uuid        PRIMARY KEY REFERENCES products (id) ON DELETE RESTRICT,
    snapshot_id           uuid        NOT NULL REFERENCES product_metadata_snapshots (id) ON DELETE RESTRICT,
    last_verified_at      timestamptz NOT NULL,
    server_time_offset_ms integer     CHECK (server_time_offset_ms IS NULL OR abs(server_time_offset_ms) < 86400000)
);

-- ------------------------------------------------------------------ transition table
CREATE TABLE allowed_transitions (
    machine       text     NOT NULL CHECK (machine IN ('pair')),
    from_state    text     NOT NULL,
    to_state      text     NOT NULL,
    actor_class   text     NOT NULL CHECK (actor_class IN ('WEB', 'HOST')),
    transition_no smallint NOT NULL,
    chain         text     NOT NULL CHECK (chain IN ('NONE', 'CSRF', 'RESTRICTIVE', 'FULL')),
    event_code    text     NOT NULL CHECK (event_code ~ '^[a-z_.]{3,64}$'),
    PRIMARY KEY (machine, from_state, to_state, actor_class)
);
INSERT INTO allowed_transitions
    (machine, from_state, to_state, actor_class, transition_no, chain, event_code) VALUES
    ('pair', 'PAUSED', 'VALIDATING', 'HOST', 2, 'CSRF', 'pair.validation_started'),
    ('pair', 'PAUSED', 'VALIDATING', 'WEB', 2, 'CSRF', 'pair.validation_started'),
    ('pair', 'PROPOSED', 'VALIDATING', 'HOST', 2, 'CSRF', 'pair.validation_started'),
    ('pair', 'PROPOSED', 'VALIDATING', 'WEB', 2, 'CSRF', 'pair.validation_started'),
    ('pair', 'RESEARCH_ONLY', 'VALIDATING', 'HOST', 2, 'CSRF', 'pair.validation_started'),
    ('pair', 'RESEARCH_ONLY', 'VALIDATING', 'WEB', 2, 'CSRF', 'pair.validation_started'),
    ('pair', 'VALIDATING', 'RESEARCH_ONLY', 'HOST', 3, 'NONE', 'pair.research_only'),
    ('pair', 'VALIDATING', 'PAPER_ELIGIBLE', 'HOST', 4, 'NONE', 'pair.paper_eligible'),
    ('pair', 'PAPER_ELIGIBLE', 'VALIDATING', 'HOST', 6, 'NONE', 'pair.eligibility_expired'),
    ('pair', 'PAPER_ELIGIBLE', 'PAPER_ACTIVE', 'WEB', 7, 'FULL', 'pair.activated_paper'),
    ('pair', 'PAPER_ACTIVE', 'PAUSED', 'HOST', 8, 'RESTRICTIVE', 'pair.paused'),
    ('pair', 'PAPER_ACTIVE', 'PAUSED', 'WEB', 8, 'RESTRICTIVE', 'pair.paused'),
    ('pair', 'PAUSED', 'PAPER_ACTIVE', 'WEB', 9, 'FULL', 'pair.resumed_paper'),
    ('pair', 'PAUSED', 'PAPER_ELIGIBLE', 'WEB', 10, 'CSRF', 'pair.deactivated'),
    ('pair', 'PAPER_ELIGIBLE', 'DISABLED', 'WEB', 12, 'FULL', 'pair.disabled'),
    ('pair', 'PAUSED', 'DISABLED', 'WEB', 12, 'FULL', 'pair.disabled'),
    ('pair', 'PROPOSED', 'DISABLED', 'WEB', 12, 'FULL', 'pair.disabled'),
    ('pair', 'RESEARCH_ONLY', 'DISABLED', 'WEB', 12, 'FULL', 'pair.disabled'),
    ('pair', 'VALIDATING', 'DISABLED', 'WEB', 12, 'FULL', 'pair.disabled'),
    ('pair', 'DISABLED', 'VALIDATING', 'WEB', 13, 'FULL', 'pair.reenabled'),
    ('pair', 'DISABLED', 'ARCHIVED', 'WEB', 14, 'FULL', 'pair.archived'),
    ('pair', 'PAPER_ELIGIBLE', 'ARCHIVED', 'WEB', 14, 'FULL', 'pair.archived'),
    ('pair', 'PAUSED', 'ARCHIVED', 'WEB', 14, 'FULL', 'pair.archived'),
    ('pair', 'PROPOSED', 'ARCHIVED', 'WEB', 14, 'FULL', 'pair.archived'),
    ('pair', 'RESEARCH_ONLY', 'ARCHIVED', 'WEB', 14, 'FULL', 'pair.archived'),
    ('pair', 'VALIDATING', 'ARCHIVED', 'WEB', 14, 'FULL', 'pair.archived');
CREATE TRIGGER allowed_transitions_immutable
    BEFORE UPDATE OR DELETE ON allowed_transitions
    FOR EACH ROW EXECUTE FUNCTION td_append_only();
CREATE TRIGGER allowed_transitions_no_truncate
    BEFORE TRUNCATE ON allowed_transitions
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- ------------------------------------------------------------------ pairs
CREATE TABLE pairs (
    id               uuid        PRIMARY KEY,
    product_uuid     uuid        NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    state            text        NOT NULL CHECK (state IN (
        'PROPOSED', 'VALIDATING', 'RESEARCH_ONLY', 'PAPER_ELIGIBLE', 'PAPER_ACTIVE',
        'PAUSED', 'DISABLED', 'ARCHIVED')),
    version          integer     NOT NULL CHECK (version >= 1),
    order_product_id text        NOT NULL CHECK (order_product_id ~ '^[A-Z0-9]+-USDC$'),
    data_product_id  text        CHECK (data_product_id IS NULL OR data_product_id ~ '^[A-Z0-9]+-(USD|USDC)$'),
    data_basis       text        NOT NULL CHECK (data_basis IN ('OWN_BOOK', 'UNIFIED_USD_BOOK', 'UNKNOWN')),
    proposed_via     text        NOT NULL CHECK (proposed_via IN ('WEB', 'HOST')),
    proposed_by      uuid        REFERENCES users (id) ON DELETE RESTRICT,
    proposed_at      timestamptz NOT NULL,
    state_changed_at timestamptz NOT NULL,
    ever_active      boolean     NOT NULL DEFAULT false,
    eligible_run_id  uuid,
    successor_of     uuid        REFERENCES pairs (id) ON DELETE RESTRICT,
    CONSTRAINT pairs_web_has_proposer CHECK (proposed_via <> 'WEB' OR proposed_by IS NOT NULL),
    CONSTRAINT pairs_active_was_active CHECK (state <> 'PAPER_ACTIVE' OR ever_active),
    CONSTRAINT pairs_active_has_run CHECK (state <> 'PAPER_ACTIVE' OR eligible_run_id IS NOT NULL)
);
-- One candidate or live pair per product; a product can be proposed again after ARCHIVED.
CREATE UNIQUE INDEX pairs_one_open_per_product ON pairs (product_uuid) WHERE state <> 'ARCHIVED';
-- At most ONE active pair in the whole system. LIVE_ACTIVE is listed so that widening the state
-- CHECK later cannot silently allow a second active pair.
CREATE UNIQUE INDEX pairs_one_active ON pairs ((true)) WHERE state IN ('PAPER_ACTIVE', 'LIVE_ACTIVE');
CREATE INDEX pairs_state_idx ON pairs (state);

-- ------------------------------------------------------------------ history and evidence
CREATE TABLE pair_state_history (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pair_id       uuid        NOT NULL REFERENCES pairs (id) ON DELETE RESTRICT,
    version_after integer     NOT NULL CHECK (version_after >= 1),
    state_before  text,
    state_after   text        NOT NULL,
    actor_class   text        NOT NULL CHECK (actor_class IN ('WEB', 'HOST')),
    actor_user_id uuid        REFERENCES users (id) ON DELETE RESTRICT,
    transition_no smallint    NOT NULL,
    reason_code   text        CHECK (char_length(reason_code) <= 64),
    occurred_at   timestamptz NOT NULL,
    request_id    text        CHECK (char_length(request_id) <= 64),
    audit_seq     bigint      CHECK (audit_seq IS NULL OR audit_seq >= 1),
    CONSTRAINT pair_state_history_unique UNIQUE (pair_id, version_after)
);
CREATE TRIGGER pair_state_history_append_only
    BEFORE UPDATE OR DELETE ON pair_state_history
    FOR EACH ROW EXECUTE FUNCTION td_append_only();
CREATE TRIGGER pair_state_history_no_truncate
    BEFORE TRUNCATE ON pair_state_history
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

CREATE TABLE pair_validation_runs (
    id                uuid        PRIMARY KEY,
    pair_id           uuid        NOT NULL REFERENCES pairs (id) ON DELETE RESTRICT,
    pair_version      integer     NOT NULL CHECK (pair_version >= 1),
    snapshot_id       uuid        NOT NULL REFERENCES product_metadata_snapshots (id) ON DELETE RESTRICT,
    started_at        timestamptz NOT NULL,
    finished_at       timestamptz NOT NULL,
    outcome           text        NOT NULL CHECK (outcome IN ('PASS', 'FAIL', 'INCONCLUSIVE')),
    checks            jsonb       NOT NULL
        CHECK (jsonb_typeof(checks) = 'array' AND octet_length(checks::text) <= 65536),
    thresholds_sha256 text        NOT NULL CHECK (thresholds_sha256 ~ '^[0-9a-f]{64}$'),
    expires_at        timestamptz NOT NULL,
    CONSTRAINT pair_validation_runs_time_order CHECK (finished_at >= started_at AND expires_at > finished_at)
);
CREATE INDEX pair_validation_runs_pair_idx ON pair_validation_runs (pair_id, started_at DESC);
CREATE TRIGGER pair_validation_runs_append_only
    BEFORE UPDATE OR DELETE ON pair_validation_runs
    FOR EACH ROW EXECUTE FUNCTION td_append_only();
CREATE TRIGGER pair_validation_runs_no_truncate
    BEFORE TRUNCATE ON pair_validation_runs
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

ALTER TABLE pairs
    ADD CONSTRAINT pairs_eligible_run_fk
    FOREIGN KEY (eligible_run_id) REFERENCES pair_validation_runs (id) ON DELETE RESTRICT;

CREATE INDEX audit_events_target_idx ON audit_events (target_type, target_id, seq DESC);

-- ------------------------------------------------------------------ lifecycle enforcement
CREATE FUNCTION pairs_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    actor text := td_actor_class();
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pairs are never deleted; archive instead'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF actor IS NULL THEN
        RAISE EXCEPTION 'pairs may only be changed by the application roles'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'PROPOSED' OR NEW.version <> 1 OR NEW.ever_active
           OR NEW.eligible_run_id IS NOT NULL THEN
            RAISE EXCEPTION 'a pair starts as PROPOSED, version 1'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.proposed_via <> actor THEN
            RAISE EXCEPTION 'proposed_via must match the acting role'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id <> OLD.id OR NEW.product_uuid <> OLD.product_uuid
       OR NEW.order_product_id <> OLD.order_product_id
       OR NEW.data_product_id IS DISTINCT FROM OLD.data_product_id
       OR NEW.data_basis <> OLD.data_basis
       OR NEW.proposed_via <> OLD.proposed_via
       OR NEW.proposed_by IS DISTINCT FROM OLD.proposed_by
       OR NEW.proposed_at <> OLD.proposed_at
       OR NEW.successor_of IS DISTINCT FROM OLD.successor_of THEN
        RAISE EXCEPTION 'identifying pair columns are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.version <> OLD.version + 1 OR NEW.state = OLD.state THEN
        RAISE EXCEPTION 'every pair update must be exactly one state transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM allowed_transitions t
        WHERE t.machine = 'pair' AND t.from_state = OLD.state
          AND t.to_state = NEW.state AND t.actor_class = actor
    ) THEN
        RAISE EXCEPTION 'transition % -> % is not allowed for %', OLD.state, NEW.state, actor
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.ever_active AND NOT NEW.ever_active THEN
        RAISE EXCEPTION 'ever_active cannot be cleared'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'PAPER_ACTIVE' AND (
        NOT NEW.ever_active OR NEW.eligible_run_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM pair_validation_runs r
            WHERE r.id = NEW.eligible_run_id AND r.pair_id = NEW.id AND r.outcome = 'PASS')
    ) THEN
        RAISE EXCEPTION 'activation requires a PASS validation run for this pair'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.eligible_run_id IS DISTINCT FROM OLD.eligible_run_id AND (
        actor <> 'HOST' OR NEW.state <> 'PAPER_ELIGIBLE' OR NOT EXISTS (
            SELECT 1 FROM pair_validation_runs r
            WHERE r.id = NEW.eligible_run_id AND r.pair_id = NEW.id AND r.outcome = 'PASS')
    ) THEN
        RAISE EXCEPTION 'only the runner may record a PASS run as eligibility evidence'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER pairs_guard_trigger
    BEFORE INSERT OR UPDATE OR DELETE ON pairs
    FOR EACH ROW EXECUTE FUNCTION pairs_guard();
CREATE TRIGGER pairs_no_truncate
    BEFORE TRUNCATE ON pairs
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- A history row may only describe the pair's current state and the caller's own actor class.
CREATE FUNCTION pair_state_history_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF NEW.actor_class IS DISTINCT FROM td_actor_class() THEN
        RAISE EXCEPTION 'history actor_class must match the acting role'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pairs p
        WHERE p.id = NEW.pair_id AND p.version = NEW.version_after AND p.state = NEW.state_after
    ) THEN
        RAISE EXCEPTION 'history must describe the pair''s current version and state'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER pair_state_history_guard_trigger
    BEFORE INSERT ON pair_state_history
    FOR EACH ROW EXECUTE FUNCTION pair_state_history_guard();

-- No state change without its history row in the same transaction.
CREATE FUNCTION pairs_require_history() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pair_state_history h
        WHERE h.pair_id = NEW.id AND h.version_after = NEW.version AND h.state_after = NEW.state
    ) THEN
        RAISE EXCEPTION 'pair change committed without a history row'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END
$fn$;
CREATE CONSTRAINT TRIGGER pairs_history_required
    AFTER INSERT OR UPDATE ON pairs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION pairs_require_history();

-- ------------------------------------------------------------------ grants
GRANT SELECT ON products, product_metadata_snapshots, product_metadata_current,
    allowed_transitions, pairs, pair_state_history, pair_validation_runs TO td_app, td_ctl;

GRANT INSERT ON products TO td_ctl;
GRANT UPDATE (discovered_rank, last_seen_at) ON products TO td_ctl;
GRANT INSERT ON product_metadata_snapshots TO td_ctl;
GRANT INSERT ON product_metadata_current TO td_ctl;
GRANT UPDATE (snapshot_id, last_verified_at, server_time_offset_ms) ON product_metadata_current TO td_ctl;

GRANT INSERT ON pairs TO td_app, td_ctl;
GRANT UPDATE (state, version, state_changed_at, ever_active, eligible_run_id) ON pairs TO td_app, td_ctl;
GRANT INSERT ON pair_state_history TO td_app, td_ctl;
GRANT INSERT ON pair_validation_runs TO td_ctl;
