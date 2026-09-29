-- Phase 6: read-only LLM review packages. The feature is DISABLED by default.
-- Write model:
--   td_app (web)  enables/disables the feature and REQUESTS packages (ADMIN chain is enforced in the
--                 application); it can also mark a READY package CORRUPT after a failed verification.
--   td_ctl (host) builds packages (REQUESTED -> GENERATING -> READY|FAILED), expires them and removes
--                 their content. It is the only role that can produce a package.
-- Packages contain no bot, pair, order, risk or configuration state, and nothing here can change any
-- of that state: these tables reference `users` only.

CREATE TABLE review_settings (
    id             boolean     PRIMARY KEY DEFAULT true CHECK (id),
    enabled        boolean     NOT NULL DEFAULT false,
    retention_days integer     NOT NULL DEFAULT 14 CHECK (retention_days BETWEEN 1 AND 90),
    updated_at     timestamptz NOT NULL
);
INSERT INTO review_settings (id, enabled, retention_days, updated_at) VALUES (true, false, 14, now());

CREATE TABLE review_packages (
    id                 uuid        PRIMARY KEY,
    state              text        NOT NULL CHECK (state IN
        ('REQUESTED', 'GENERATING', 'READY', 'FAILED', 'CORRUPT', 'EXPIRED')),
    period_start       date        NOT NULL,
    period_end         date        NOT NULL,
    scope              text[]      NOT NULL,
    retention_days     integer     NOT NULL CHECK (retention_days BETWEEN 1 AND 90),
    requested_by       uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    requested_at       timestamptz NOT NULL,
    started_at         timestamptz,
    finished_at        timestamptz,
    expires_at         timestamptz,
    storage_name       text        CHECK (storage_name ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.zip$'),
    size_bytes         bigint      CHECK (size_bytes > 0 AND size_bytes <= 52428800),
    file_count         integer     CHECK (file_count BETWEEN 1 AND 64),
    package_sha256     text        CHECK (package_sha256 ~ '^[0-9a-f]{64}$'),
    manifest_sha256    text        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    config_sha256      text        CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    exporter_version   text        CHECK (char_length(exporter_version) <= 32),
    failure_code       text        CHECK (failure_code ~ '^[A-Z_]{3,40}$'),
    files              jsonb       CHECK (octet_length(files::text) <= 65536),
    content_removed_at timestamptz,
    CONSTRAINT review_period CHECK (period_end >= period_start AND period_end - period_start <= 90),
    CONSTRAINT review_scope CHECK (
        cardinality(scope) BETWEEN 1 AND 6
        AND scope <@ ARRAY['backtests', 'paper', 'pairs', 'data_quality', 'grid_plans', 'audit_summary']::text[]),
    CONSTRAINT review_ready_is_complete CHECK (
        state <> 'READY' OR (storage_name IS NOT NULL AND size_bytes IS NOT NULL
            AND package_sha256 IS NOT NULL AND manifest_sha256 IS NOT NULL
            AND expires_at IS NOT NULL AND files IS NOT NULL)),
    CONSTRAINT review_failed_has_code CHECK (state <> 'FAILED' OR failure_code IS NOT NULL)
);
CREATE INDEX review_packages_recent_idx ON review_packages (requested_at DESC, id);
-- Single flight: at most one package waiting or being built.
CREATE UNIQUE INDEX review_single_flight ON review_packages ((true))
    WHERE state IN ('REQUESTED', 'GENERATING');

CREATE FUNCTION review_package_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    actor text := td_actor_class();
    retained integer;
    recent integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'review packages are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF actor IS DISTINCT FROM 'WEB' THEN
            RAISE EXCEPTION 'only the web tier requests review packages' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.state <> 'REQUESTED' THEN
            RAISE EXCEPTION 'a review package starts REQUESTED' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NOT (SELECT enabled FROM review_settings WHERE id) THEN
            RAISE EXCEPTION 'review packages are disabled' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT count(*) INTO retained FROM review_packages
            WHERE state IN ('REQUESTED', 'GENERATING', 'READY', 'CORRUPT');
        IF retained >= 10 THEN
            RAISE EXCEPTION 'at most 10 retained review packages' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT count(*) INTO recent FROM review_packages WHERE requested_at > NEW.requested_at - interval '1 hour';
        IF recent >= 3 THEN
            RAISE EXCEPTION 'at most 3 review package requests per hour' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.storage_name IS NOT NULL OR NEW.package_sha256 IS NOT NULL OR NEW.files IS NOT NULL THEN
            RAISE EXCEPTION 'a new request carries no package content' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.period_start <> OLD.period_start OR NEW.period_end <> OLD.period_end
       OR NEW.scope <> OLD.scope OR NEW.requested_by <> OLD.requested_by
       OR NEW.requested_at <> OLD.requested_at OR NEW.retention_days <> OLD.retention_days THEN
        RAISE EXCEPTION 'review package request fields are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.state = OLD.state THEN
        IF actor = 'HOST' AND OLD.state = 'EXPIRED' AND OLD.content_removed_at IS NULL THEN
            RETURN NEW;  -- recording that the content was removed
        END IF;
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'review package changes only by transition' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT (
        (actor = 'HOST' AND OLD.state = 'REQUESTED' AND NEW.state IN ('GENERATING', 'FAILED'))
        OR (actor = 'HOST' AND OLD.state = 'GENERATING' AND NEW.state IN ('READY', 'FAILED'))
        OR (actor = 'HOST' AND OLD.state = 'READY' AND NEW.state IN ('EXPIRED', 'CORRUPT'))
        OR (actor = 'HOST' AND OLD.state = 'CORRUPT' AND NEW.state = 'EXPIRED')
        OR (actor = 'HOST' AND OLD.state = 'FAILED' AND NEW.state = 'EXPIRED')
        OR (actor = 'WEB' AND OLD.state = 'READY' AND NEW.state = 'CORRUPT')
    ) THEN
        RAISE EXCEPTION 'review package transition % -> % is not allowed for %', OLD.state, NEW.state, actor
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER review_packages_guard BEFORE INSERT OR UPDATE OR DELETE ON review_packages
    FOR EACH ROW EXECUTE FUNCTION review_package_guard();
CREATE TRIGGER review_packages_no_truncate BEFORE TRUNCATE ON review_packages
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

CREATE FUNCTION review_settings_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'review settings are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS DISTINCT FROM 'WEB' THEN
        RAISE EXCEPTION 'only the web tier changes review settings' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER review_settings_guard_trigger BEFORE UPDATE OR DELETE ON review_settings
    FOR EACH ROW EXECUTE FUNCTION review_settings_guard();
CREATE TRIGGER review_settings_no_truncate BEFORE TRUNCATE ON review_settings
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

GRANT SELECT ON review_settings, review_packages TO td_app, td_ctl;
GRANT UPDATE (enabled, retention_days, updated_at) ON review_settings TO td_app;
GRANT INSERT ON review_packages TO td_app;
GRANT UPDATE (state, finished_at, failure_code) ON review_packages TO td_app;
GRANT UPDATE (state, started_at, finished_at, expires_at, storage_name, size_bytes, file_count,
    package_sha256, manifest_sha256, config_sha256, exporter_version, failure_code, files,
    content_removed_at) ON review_packages TO td_ctl;
