-- Development/test rollback of 0001_auth.sql. DESTROYS users, sessions and the audit log.
DROP TABLE IF EXISTS audit_head;
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS login_attempts;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS schema_meta;
DROP FUNCTION IF EXISTS audit_head_guard();
DROP FUNCTION IF EXISTS audit_events_reject_change();
