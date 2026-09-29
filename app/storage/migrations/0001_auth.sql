-- Phase 2: authentication, sessions, login throttling and the tamper-evident audit log.
-- Applied by `python -m app.storage.database migrate` as the database owner. Roles td_app and
-- td_ctl already exist when this file runs (created by the runner).
-- Least privilege: the web role (td_app) cannot create users, change roles, or alter audit rows.

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO td_app, td_ctl;

CREATE TABLE schema_meta (
    id         boolean     PRIMARY KEY DEFAULT true CHECK (id),
    version    integer     NOT NULL CHECK (version >= 1),
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id                  uuid        PRIMARY KEY,
    username            text        NOT NULL CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'),
    role                text        NOT NULL CHECK (role IN ('ADMIN', 'VIEWER')),
    password_hash       text        NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
    password_changed_at timestamptz NOT NULL,
    created_at          timestamptz NOT NULL,
    disabled_at         timestamptz,
    CONSTRAINT users_username_unique UNIQUE (username)
);

CREATE TABLE sessions (
    id                  uuid        PRIMARY KEY,
    user_id             uuid        NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    token_hash          text        NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    created_at          timestamptz NOT NULL,
    last_seen_at        timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at          timestamptz,
    revoked_reason      text,
    reauth_at           timestamptz,
    CONSTRAINT sessions_token_hash_unique UNIQUE (token_hash),
    CONSTRAINT sessions_expiry_after_creation CHECK (absolute_expires_at > created_at),
    CONSTRAINT sessions_revocation_consistent CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL)),
    CONSTRAINT sessions_revoked_reason_known CHECK (
        revoked_reason IS NULL OR revoked_reason IN (
            'LOGOUT', 'IDLE_TIMEOUT', 'ABSOLUTE_EXPIRY', 'ADMIN_REVOKED', 'SELF_REVOKED',
            'PASSWORD_CHANGED', 'ROTATED', 'SESSION_LIMIT', 'USER_DISABLED')
    )
);
CREATE INDEX sessions_user_active_idx ON sessions (user_id) WHERE revoked_at IS NULL;

CREATE TABLE login_attempts (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope        text        NOT NULL CHECK (scope IN ('PAIR', 'CLIENT', 'ACCOUNT')),
    key_hmac     text        NOT NULL CHECK (key_hmac ~ '^[0-9a-f]{64}$'),
    success      boolean     NOT NULL,
    attempted_at timestamptz NOT NULL
);
CREATE INDEX login_attempts_lookup_idx ON login_attempts (scope, key_hmac, attempted_at DESC);
CREATE INDEX login_attempts_age_idx ON login_attempts (attempted_at);

CREATE TABLE audit_events (
    seq           bigint      PRIMARY KEY CHECK (seq >= 1),
    event_id      uuid        NOT NULL,
    occurred_at   timestamptz NOT NULL,
    event_code    text        NOT NULL CHECK (event_code ~ '^[a-z_.]{3,64}$'),
    result        text        NOT NULL CHECK (result IN ('SUCCESS', 'FAILURE', 'DENIED')),
    actor_user_id uuid        REFERENCES users (id) ON DELETE RESTRICT,
    actor_role    text        CHECK (actor_role IN ('ADMIN', 'VIEWER', 'HOST_CLI')),
    target_type   text        CHECK (char_length(target_type) <= 64),
    target_id     text        CHECK (char_length(target_id) <= 128),
    reason_code   text        CHECK (char_length(reason_code) <= 64),
    client_tag    text        CHECK (char_length(client_tag) <= 32),
    request_id    text        CHECK (char_length(request_id) <= 64),
    detail        text        NOT NULL CHECK (octet_length(detail) <= 4096),
    prev_hash     text        NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    event_hash    text        NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT audit_events_event_id_unique UNIQUE (event_id),
    CONSTRAINT audit_events_hash_unique UNIQUE (event_hash)
);
CREATE INDEX audit_events_code_idx ON audit_events (event_code, seq DESC);
CREATE INDEX audit_events_recent_idx ON audit_events (event_code, client_tag, occurred_at DESC);

CREATE TABLE audit_head (
    id        boolean PRIMARY KEY DEFAULT true CHECK (id),
    last_seq  bigint  NOT NULL CHECK (last_seq >= 0),
    last_hash text    NOT NULL CHECK (last_hash ~ '^[0-9a-f]{64}$')
);
INSERT INTO audit_head (id, last_seq, last_hash) VALUES (true, 0, repeat('0', 64));

-- Append-only enforcement, effective for every role including the owner.
CREATE FUNCTION audit_events_reject_change() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS
$fn$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = 'integrity_constraint_violation';
END
$fn$;
CREATE TRIGGER audit_events_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_reject_change();
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_reject_change();

-- The head may only advance by exactly one and can never be deleted.
CREATE FUNCTION audit_head_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS
$fn$
BEGIN
    IF TG_OP <> 'UPDATE' OR NEW.last_seq <> OLD.last_seq + 1 THEN
        RAISE EXCEPTION 'audit_head may only advance by one' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER audit_head_advance_only
    BEFORE UPDATE OR DELETE ON audit_head
    FOR EACH ROW EXECUTE FUNCTION audit_head_guard();
CREATE TRIGGER audit_head_no_truncate
    BEFORE TRUNCATE ON audit_head
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_reject_change();

-- Grants. td_app is the web role; td_ctl is the local host CLI role.
GRANT SELECT ON schema_meta TO td_app, td_ctl;

GRANT SELECT ON users TO td_app, td_ctl;
GRANT INSERT ON users TO td_ctl;
GRANT UPDATE (password_hash, password_changed_at) ON users TO td_app;
GRANT UPDATE (password_hash, password_changed_at, disabled_at) ON users TO td_ctl;

GRANT SELECT, INSERT ON sessions TO td_app;
GRANT SELECT ON sessions TO td_ctl;
GRANT UPDATE (last_seen_at, revoked_at, revoked_reason, reauth_at) ON sessions TO td_app, td_ctl;

GRANT SELECT, INSERT, DELETE ON login_attempts TO td_app;

GRANT SELECT, INSERT ON audit_events TO td_app, td_ctl;
GRANT SELECT, UPDATE ON audit_head TO td_app, td_ctl;
