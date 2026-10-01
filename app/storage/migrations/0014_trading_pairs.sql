-- The pairs a mode trades are chosen by name (Bot page) instead of only counted. trading_pairs is
-- the selection; trading_config.max_pairs stays as its size (at least 1), so every money rule that
-- reads max_pairs is unchanged. The live runner trades a pair only if it is selected for LIVE and
-- active; selecting a pair never activates it and never places an order.
CREATE TABLE trading_pairs (
    mode    text NOT NULL REFERENCES trading_config (mode),
    pair_id uuid NOT NULL REFERENCES pairs (id) ON DELETE RESTRICT,
    PRIMARY KEY (mode, pair_id)
);

-- What trades today keeps trading: the active pairs are selected for both modes.
INSERT INTO trading_pairs (mode, pair_id)
SELECT c.mode, p.id
FROM trading_config c
CROSS JOIN LATERAL (
    SELECT id FROM pairs WHERE state = 'PAPER_ACTIVE' ORDER BY proposed_at, id LIMIT c.max_pairs
) p;

CREATE FUNCTION trading_pairs_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'a selected pair is added or removed, never changed' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF td_actor_class() IS NULL THEN
        RAISE EXCEPTION 'unknown database role for the pair selection' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (SELECT bot_state FROM bot_control WHERE id) <> 'PAUSED' THEN
        RAISE EXCEPTION 'the pair selection changes only while the bot is PAUSED' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' AND (SELECT state FROM pairs WHERE id = NEW.pair_id)
            NOT IN ('PAPER_ELIGIBLE', 'PAPER_ACTIVE', 'PAUSED') THEN
        RAISE EXCEPTION 'only a validated pair can be selected' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END
$fn$;
CREATE TRIGGER trading_pairs_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON trading_pairs
    FOR EACH ROW EXECUTE FUNCTION trading_pairs_guard();
CREATE TRIGGER trading_pairs_no_truncate BEFORE TRUNCATE ON trading_pairs
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- The selection never holds more pairs than max_pairs, the number the invested-cap rule
-- (quote_per_grid * max_pairs <= invested_cap) was checked with. Checked at commit, from both sides.
CREATE FUNCTION trading_pairs_fit() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    m text := NEW.mode;
BEGIN
    IF (SELECT count(*) FROM trading_pairs WHERE mode = m)
            > (SELECT max_pairs FROM trading_config WHERE mode = m) THEN
        RAISE EXCEPTION 'more pairs are selected than the trading configuration counts' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END
$fn$;
CREATE CONSTRAINT TRIGGER trading_pairs_fit_trigger AFTER INSERT ON trading_pairs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trading_pairs_fit();
CREATE CONSTRAINT TRIGGER trading_config_pairs_fit_trigger AFTER UPDATE ON trading_config
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION trading_pairs_fit();

GRANT SELECT, INSERT, DELETE ON trading_pairs TO td_app, td_ctl;
