-- Development/test rollback of 0004_review.sql. DESTROYS review package rows and settings.
-- Package files on disk are NOT removed (delete them by hand). Audit events stay (append-only).
DROP TABLE IF EXISTS review_packages;
DROP TABLE IF EXISTS review_settings;
DROP FUNCTION IF EXISTS review_package_guard();
DROP FUNCTION IF EXISTS review_settings_guard();
