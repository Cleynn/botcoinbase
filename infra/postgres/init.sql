-- Runs once on first initialisation, as the bootstrap superuser, against POSTGRES_DB=tradingdots.
-- Phase 1 creates no tables and no application roles: only tightens defaults.
REVOKE ALL ON DATABASE tradingdots FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
