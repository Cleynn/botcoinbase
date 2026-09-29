-- Development/test rollback of 0005_proposals.sql. DESTROYS proposal rows, change requests and
-- attestations. Proposal files on disk are NOT removed (delete them by hand). Audit events stay.
DROP TABLE IF EXISTS proposal_attestations;
DROP TABLE IF EXISTS change_requests;
DROP TABLE IF EXISTS proposal_state_history;
DROP TABLE IF EXISTS proposals CASCADE;
DROP TABLE IF EXISTS proposal_settings;
DROP FUNCTION IF EXISTS proposals_require_records();
DROP FUNCTION IF EXISTS proposal_guard();
DROP FUNCTION IF EXISTS proposal_settings_guard();
