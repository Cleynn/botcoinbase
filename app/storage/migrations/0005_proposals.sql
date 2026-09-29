-- Phase 7: import and review of UNTRUSTED ADVISORY LLM proposals. Import is DISABLED by default.
-- A proposal never changes anything: approval creates a tracked MANUAL change request (a record),
-- and every later step is an ADMIN attestation about work done elsewhere. Nothing here references a
-- pair, order, ledger, strategy or configuration table.
-- Write model:
--   td_app (web)  enables/disables import, stores a new IMPORTED proposal (bytes already written to the
--                 proposals volume under a generated name), performs the ADMIN review steps.
--   td_ctl (host) validates: IMPORTED -> VALIDATING -> VALIDATED | REJECTED, fills in the parsed
--                 fields, findings and risk assessment, and removes purged content.

CREATE TABLE proposal_settings (
    id             boolean     PRIMARY KEY DEFAULT true CHECK (id),
    import_enabled boolean     NOT NULL DEFAULT false,
    updated_at     timestamptz NOT NULL
);
INSERT INTO proposal_settings (id, import_enabled, updated_at) VALUES (true, false, now());

CREATE TABLE proposals (
    id                    uuid        PRIMARY KEY,
    state                 text        NOT NULL CHECK (state IN (
        'IMPORTED', 'VALIDATING', 'VALIDATED', 'REJECTED', 'REVIEWED', 'CHANGE_REQUEST_CREATED',
        'IMPLEMENTED', 'BACKTESTED', 'PAPER_VALIDATED', 'CLOSED')),
    storage_name          text        NOT NULL
        CHECK (storage_name ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.proposal$'),
    size_bytes            integer     NOT NULL CHECK (size_bytes BETWEEN 2 AND 131072),
    sha256                text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    declared_mime         text        NOT NULL CHECK (declared_mime IN ('text/plain', 'application/json')),
    imported_by           uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    imported_at           timestamptz NOT NULL,
    -- set once by the host validator
    proposal_ref          text        CHECK (proposal_ref ~ '^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$'),
    category              text        CHECK (category IN (
        'strategy', 'risk', 'pair_research', 'data', 'backtest', 'paper_execution', 'monitoring',
        'documentation', 'code_quality')),
    linked_package_id     uuid        REFERENCES review_packages (id) ON DELETE RESTRICT,
    linked_package_sha256 text        CHECK (linked_package_sha256 ~ '^[0-9a-f]{64}$'),
    parsed                jsonb       CHECK (octet_length(parsed::text) <= 65536),
    findings              jsonb       CHECK (octet_length(findings::text) <= 32768),
    risk_assessment       jsonb       CHECK (octet_length(risk_assessment::text) <= 8192),
    reject_rules          text[]      CHECK (cardinality(reject_rules) <= 64),
    validated_at          timestamptz,
    -- ADMIN review
    review_notes          text        CHECK (char_length(review_notes) <= 1000),
    reviewed_by           uuid        REFERENCES users (id) ON DELETE RESTRICT,
    reviewed_at           timestamptz,
    closed_reason         text        CHECK (closed_reason IN (
        'COMPLETED', 'NOT_PURSUED', 'SUPERSEDED', 'DUPLICATE', 'UNSAFE', 'OTHER')),
    closed_at             timestamptz,
    content_removed_at    timestamptz,
    CONSTRAINT proposals_storage_unique UNIQUE (storage_name),
    CONSTRAINT proposals_validated_has_parse CHECK (
        state NOT IN ('VALIDATED', 'REVIEWED', 'CHANGE_REQUEST_CREATED', 'IMPLEMENTED',
                      'BACKTESTED', 'PAPER_VALIDATED')
        OR (parsed IS NOT NULL AND category IS NOT NULL AND linked_package_id IS NOT NULL
            AND risk_assessment IS NOT NULL)),
    CONSTRAINT proposals_rejected_has_rules CHECK (state <> 'REJECTED' OR reject_rules IS NOT NULL),
    CONSTRAINT proposals_closed_has_reason CHECK (state <> 'CLOSED' OR closed_reason IS NOT NULL)
);
CREATE INDEX proposals_recent_idx ON proposals (imported_at DESC, id);
-- The same bytes cannot be imported twice while a proposal for them is live; after REJECTED or CLOSED
-- a resubmission is a new import.
CREATE UNIQUE INDEX proposals_sha_live ON proposals (sha256) WHERE state NOT IN ('REJECTED', 'CLOSED');

CREATE TABLE proposal_state_history (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proposal_id   uuid        NOT NULL REFERENCES proposals (id) ON DELETE RESTRICT,
    state_before  text,
    state_after   text        NOT NULL,
    actor_class   text        NOT NULL CHECK (actor_class IN ('WEB', 'HOST')),
    actor_user_id uuid        REFERENCES users (id) ON DELETE RESTRICT,
    reason_code   text        CHECK (reason_code ~ '^[A-Z_]{2,40}$'),
    occurred_at   timestamptz NOT NULL,
    CONSTRAINT proposal_history_once UNIQUE (proposal_id, state_after)
);

CREATE TABLE change_requests (
    id                  uuid        PRIMARY KEY,
    seq                 bigint      GENERATED ALWAYS AS IDENTITY UNIQUE,
    proposal_id         uuid        NOT NULL UNIQUE REFERENCES proposals (id) ON DELETE RESTRICT,
    change_type         text        NOT NULL CHECK (change_type IN (
        'PARAMETER_CHANGE', 'CODE_CHANGE', 'CONFIG_RELEASE', 'DOCUMENTATION', 'MONITORING',
        'RESEARCH_ONLY')),
    impact_assessment   text        NOT NULL CHECK (char_length(impact_assessment) BETWEEN 20 AND 2000),
    ceilings_unaffected boolean     NOT NULL,
    created_by          uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    created_at          timestamptz NOT NULL,
    CONSTRAINT change_requests_ceilings CHECK (change_type <> 'PARAMETER_CHANGE' OR ceilings_unaffected)
);

CREATE TABLE proposal_attestations (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proposal_id  uuid        NOT NULL REFERENCES proposals (id) ON DELETE RESTRICT,
    kind         text        NOT NULL CHECK (kind IN ('IMPLEMENTED', 'BACKTESTED', 'PAPER_VALIDATED')),
    reference    text        CHECK (reference ~ '^[A-Za-z0-9][A-Za-z0-9._/@-]{2,63}$'),
    report_ids   uuid[]      CHECK (cardinality(report_ids) BETWEEN 1 AND 5),
    attested_by  uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    attested_at  timestamptz NOT NULL,
    CONSTRAINT proposal_attestation_once UNIQUE (proposal_id, kind),
    CONSTRAINT proposal_attestation_shape CHECK (
        (kind = 'IMPLEMENTED' AND reference IS NOT NULL AND report_ids IS NULL)
        OR (kind <> 'IMPLEMENTED' AND report_ids IS NOT NULL AND reference IS NULL))
);

-- ------------------------------------------------------------------ append-only records
CREATE TRIGGER proposal_history_append_only BEFORE UPDATE OR DELETE ON proposal_state_history
    FOR EACH ROW EXECUTE FUNCTION td_append_only();
CREATE TRIGGER proposal_history_no_truncate BEFORE TRUNCATE ON proposal_state_history
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();
CREATE TRIGGER change_requests_append_only BEFORE UPDATE OR DELETE ON change_requests
    FOR EACH ROW EXECUTE FUNCTION td_append_only();
CREATE TRIGGER change_requests_no_truncate BEFORE TRUNCATE ON change_requests
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();
CREATE TRIGGER proposal_attestations_append_only BEFORE UPDATE OR DELETE ON proposal_attestations
    FOR EACH ROW EXECUTE FUNCTION td_append_only();
CREATE TRIGGER proposal_attestations_no_truncate BEFORE TRUNCATE ON proposal_attestations
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- ------------------------------------------------------------------ settings guard
CREATE FUNCTION proposal_settings_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'proposal settings are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS DISTINCT FROM 'WEB' THEN
        RAISE EXCEPTION 'only the web tier changes proposal settings' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER proposal_settings_guard_trigger BEFORE UPDATE OR DELETE ON proposal_settings
    FOR EACH ROW EXECUTE FUNCTION proposal_settings_guard();
CREATE TRIGGER proposal_settings_no_truncate BEFORE TRUNCATE ON proposal_settings
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- ------------------------------------------------------------------ proposal state machine
CREATE FUNCTION proposal_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    actor text := td_actor_class();
    hour_n integer;
    day_n integer;
    total_bytes bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'proposals are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF actor IS DISTINCT FROM 'WEB' THEN
            RAISE EXCEPTION 'only the web tier imports proposals' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state <> 'IMPORTED' THEN
            RAISE EXCEPTION 'a proposal starts IMPORTED' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT (SELECT import_enabled FROM proposal_settings WHERE id) THEN
            RAISE EXCEPTION 'proposal import is disabled' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.parsed IS NOT NULL OR NEW.findings IS NOT NULL OR NEW.category IS NOT NULL
           OR NEW.linked_package_id IS NOT NULL OR NEW.review_notes IS NOT NULL
           OR NEW.closed_reason IS NOT NULL OR NEW.reject_rules IS NOT NULL
           OR NEW.risk_assessment IS NOT NULL THEN
            RAISE EXCEPTION 'a new proposal carries no validation or review data' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT count(*) INTO hour_n FROM proposals WHERE imported_at > NEW.imported_at - interval '1 hour';
        SELECT count(*) INTO day_n FROM proposals WHERE imported_at > NEW.imported_at - interval '1 day';
        SELECT COALESCE(sum(size_bytes), 0) INTO total_bytes FROM proposals WHERE content_removed_at IS NULL;
        IF hour_n >= 10 OR day_n >= 20 THEN
            RAISE EXCEPTION 'proposal import rate limit (10 per hour, 20 per day)' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF total_bytes + NEW.size_bytes > 209715200 THEN
            RAISE EXCEPTION 'proposal storage limit (200 MB)' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    -- identity and stored-bytes metadata never change
    IF NEW.id <> OLD.id OR NEW.storage_name <> OLD.storage_name OR NEW.size_bytes <> OLD.size_bytes
       OR NEW.sha256 <> OLD.sha256 OR NEW.declared_mime <> OLD.declared_mime
       OR NEW.imported_by <> OLD.imported_by OR NEW.imported_at <> OLD.imported_at THEN
        RAISE EXCEPTION 'proposal identity and stored bytes are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- validation results are written once, by the host, when validation finishes
    IF (NEW.parsed IS DISTINCT FROM OLD.parsed OR NEW.findings IS DISTINCT FROM OLD.findings
        OR NEW.reject_rules IS DISTINCT FROM OLD.reject_rules OR NEW.category IS DISTINCT FROM OLD.category
        OR NEW.risk_assessment IS DISTINCT FROM OLD.risk_assessment
        OR NEW.proposal_ref IS DISTINCT FROM OLD.proposal_ref
        OR NEW.linked_package_id IS DISTINCT FROM OLD.linked_package_id
        OR NEW.linked_package_sha256 IS DISTINCT FROM OLD.linked_package_sha256
        OR NEW.validated_at IS DISTINCT FROM OLD.validated_at)
       AND NOT (actor = 'HOST' AND OLD.state = 'VALIDATING' AND NEW.state IN ('VALIDATED', 'REJECTED')) THEN
        RAISE EXCEPTION 'validation results are written once by the host validator' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.review_notes IS DISTINCT FROM OLD.review_notes OR NEW.reviewed_by IS DISTINCT FROM OLD.reviewed_by
        OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at)
       AND NOT (actor = 'WEB' AND OLD.state = 'VALIDATED' AND NEW.state = 'REVIEWED') THEN
        RAISE EXCEPTION 'review notes are written once, at review' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.closed_reason IS DISTINCT FROM OLD.closed_reason OR NEW.closed_at IS DISTINCT FROM OLD.closed_at)
       AND NOT (actor = 'WEB' AND NEW.state = 'CLOSED' AND OLD.state <> 'CLOSED') THEN
        RAISE EXCEPTION 'a close reason is written once, when closing' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.content_removed_at IS DISTINCT FROM OLD.content_removed_at THEN
        IF NOT (actor = 'HOST' AND OLD.content_removed_at IS NULL AND OLD.state IN ('CLOSED', 'REJECTED')) THEN
            RAISE EXCEPTION 'only the host removes content, and only of CLOSED or REJECTED proposals' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;
    IF NOT (
        (actor = 'HOST' AND OLD.state = 'IMPORTED' AND NEW.state = 'VALIDATING')
        OR (actor = 'HOST' AND OLD.state = 'VALIDATING' AND NEW.state IN ('VALIDATED', 'REJECTED'))
        OR (actor = 'WEB' AND OLD.state = 'VALIDATED' AND NEW.state = 'REVIEWED')
        OR (actor = 'WEB' AND OLD.state = 'REVIEWED' AND NEW.state = 'CHANGE_REQUEST_CREATED')
        OR (actor = 'WEB' AND OLD.state = 'CHANGE_REQUEST_CREATED' AND NEW.state = 'IMPLEMENTED')
        OR (actor = 'WEB' AND OLD.state = 'IMPLEMENTED' AND NEW.state = 'BACKTESTED')
        OR (actor = 'WEB' AND OLD.state = 'BACKTESTED' AND NEW.state = 'PAPER_VALIDATED')
        OR (actor = 'WEB' AND OLD.state = 'PAPER_VALIDATED' AND NEW.state = 'CLOSED')
        OR (actor = 'WEB' AND OLD.state IN ('REJECTED', 'REVIEWED', 'CHANGE_REQUEST_CREATED')
            AND NEW.state = 'CLOSED')
    ) THEN
        RAISE EXCEPTION 'proposal transition % -> % is not allowed for %', OLD.state, NEW.state, actor
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER proposals_guard BEFORE INSERT OR UPDATE OR DELETE ON proposals
    FOR EACH ROW EXECUTE FUNCTION proposal_guard();
CREATE TRIGGER proposals_no_truncate BEFORE TRUNCATE ON proposals
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- Every state change leaves a history row, and the states that carry a record need it.
CREATE FUNCTION proposals_require_records() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM proposal_state_history h
                   WHERE h.proposal_id = NEW.id AND h.state_after = NEW.state) THEN
        RAISE EXCEPTION 'proposal change committed without a history row' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = 'CHANGE_REQUEST_CREATED'
       AND NOT EXISTS (SELECT 1 FROM change_requests c WHERE c.proposal_id = NEW.id) THEN
        RAISE EXCEPTION 'CHANGE_REQUEST_CREATED needs a change request record' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state IN ('IMPLEMENTED', 'BACKTESTED', 'PAPER_VALIDATED')
       AND NOT EXISTS (SELECT 1 FROM proposal_attestations a WHERE a.proposal_id = NEW.id AND a.kind = NEW.state) THEN
        RAISE EXCEPTION '% needs an attestation record', NEW.state USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END
$fn$;
CREATE CONSTRAINT TRIGGER proposals_records_required
    AFTER INSERT OR UPDATE ON proposals
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION proposals_require_records();

-- ------------------------------------------------------------------ grants
GRANT SELECT ON proposal_settings, proposals, proposal_state_history, change_requests,
    proposal_attestations TO td_app, td_ctl;
GRANT UPDATE (import_enabled, updated_at) ON proposal_settings TO td_app;
GRANT INSERT ON proposals TO td_app;
GRANT UPDATE (state, review_notes, reviewed_by, reviewed_at, closed_reason, closed_at) ON proposals TO td_app;
GRANT UPDATE (state, proposal_ref, category, linked_package_id, linked_package_sha256, parsed,
    findings, risk_assessment, reject_rules, validated_at, content_removed_at) ON proposals TO td_ctl;
GRANT INSERT ON proposal_state_history TO td_app, td_ctl;
GRANT INSERT ON change_requests, proposal_attestations TO td_app;
