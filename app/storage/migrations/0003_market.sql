-- Phase 5: candles and ingestion, frozen dataset snapshots, backtests, reports, local paper exchange.
-- Write model: td_ctl (host CLI: importer, snapshotter, backtester, paper runner) writes everything
-- here; td_app (web) only reads. Nothing in this migration can reach an exchange: the paper venue is a
-- database, and every paper table is CHECKed to the PAPER venue.

-- ------------------------------------------------------------------ ingestion
CREATE TABLE ingest_runs (
    id                    bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_uuid          uuid        NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    granularity           text        NOT NULL CHECK (granularity IN ('FIVE_MINUTE')),
    window_start          bigint      NOT NULL CHECK (window_start >= 0 AND window_start % 300 = 0),
    window_end            bigint      NOT NULL,
    status                text        NOT NULL CHECK (status IN ('COMPLETE', 'PARTIAL', 'FAILED')),
    requests              integer     NOT NULL CHECK (requests >= 0),
    retries               integer     NOT NULL CHECK (retries >= 0),
    fetched               integer     NOT NULL CHECK (fetched >= 0),
    inserted              integer     NOT NULL CHECK (inserted >= 0),
    duplicates            integer     NOT NULL CHECK (duplicates >= 0),
    conflicts             integer     NOT NULL CHECK (conflicts >= 0),
    malformed             integer     NOT NULL CHECK (malformed >= 0),
    gaps                  integer     NOT NULL CHECK (gaps >= 0),
    missing               integer     NOT NULL CHECK (missing >= 0),
    server_time_offset_ms integer,
    started_at            timestamptz NOT NULL,
    finished_at           timestamptz NOT NULL,
    CONSTRAINT ingest_runs_window CHECK (window_end > window_start AND window_end % 300 = 0),
    CONSTRAINT ingest_runs_time CHECK (finished_at >= started_at)
);
CREATE INDEX ingest_runs_product_idx ON ingest_runs (product_uuid, granularity, id DESC);

CREATE TABLE candles (
    product_uuid  uuid        NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    granularity   text        NOT NULL CHECK (granularity IN ('FIVE_MINUTE')),
    start_ts      bigint      NOT NULL CHECK (start_ts >= 0 AND start_ts % 300 = 0),
    open          numeric     NOT NULL CHECK (open > 0 AND open < 1e18),
    high          numeric     NOT NULL CHECK (high > 0 AND high < 1e18),
    low           numeric     NOT NULL CHECK (low > 0 AND low < 1e18),
    close         numeric     NOT NULL CHECK (close > 0 AND close < 1e18),
    volume        numeric     NOT NULL CHECK (volume >= 0 AND volume < 1e30),
    ingest_run_id bigint      NOT NULL REFERENCES ingest_runs (id) ON DELETE RESTRICT,
    PRIMARY KEY (product_uuid, granularity, start_ts),
    CONSTRAINT candles_ohlc CHECK (low <= open AND low <= close AND high >= open AND high >= close AND low <= high)
);

CREATE TABLE candle_conflicts (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_uuid  uuid        NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    granularity   text        NOT NULL,
    start_ts      bigint      NOT NULL,
    stored        text        NOT NULL CHECK (char_length(stored) <= 300),
    incoming      text        NOT NULL CHECK (char_length(incoming) <= 300),
    ingest_run_id bigint      NOT NULL REFERENCES ingest_runs (id) ON DELETE RESTRICT
);

CREATE TABLE data_quality_events (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingest_run_id bigint      NOT NULL REFERENCES ingest_runs (id) ON DELETE RESTRICT,
    code          text        NOT NULL CHECK (code IN (
        'GAP', 'DUPLICATE', 'CONFLICT', 'ORDER', 'MALFORMED', 'INVALID_OHLC', 'MISALIGNED',
        'FUTURE', 'OUT_OF_WINDOW', 'OPEN_CANDLE', 'FETCH_ERROR')),
    severity      text        NOT NULL CHECK (severity IN ('INFO', 'WARN', 'ERROR')),
    start_ts      bigint,
    end_ts        bigint,
    count         integer     NOT NULL CHECK (count >= 0),
    detail        text        NOT NULL CHECK (char_length(detail) <= 200)
);
CREATE INDEX data_quality_events_run_idx ON data_quality_events (ingest_run_id);
CREATE INDEX data_quality_events_code_idx ON data_quality_events (code);

CREATE TABLE ingest_cursors (
    product_uuid  uuid        NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    granularity   text        NOT NULL CHECK (granularity IN ('FIVE_MINUTE')),
    covered_until bigint      NOT NULL CHECK (covered_until >= 0 AND covered_until % 300 = 0),
    last_run_id   bigint      NOT NULL REFERENCES ingest_runs (id) ON DELETE RESTRICT,
    updated_at    timestamptz NOT NULL,
    PRIMARY KEY (product_uuid, granularity)
);

-- ------------------------------------------------------------------ frozen datasets
CREATE TABLE dataset_snapshots (
    id                   uuid        PRIMARY KEY,
    product_uuid         uuid        NOT NULL REFERENCES products (id) ON DELETE RESTRICT,
    granularity          text        NOT NULL CHECK (granularity IN ('FIVE_MINUTE')),
    range_start          bigint      NOT NULL CHECK (range_start % 300 = 0),
    range_end            bigint      NOT NULL CHECK (range_end % 300 = 0),
    candle_count         integer     NOT NULL CHECK (candle_count >= 1),
    gap_count            integer     NOT NULL CHECK (gap_count >= 0),
    missing_count        integer     NOT NULL CHECK (missing_count >= 0),
    max_ingest_run_id    bigint      NOT NULL,
    file_sha256          text        NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    content_sha256       text        NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    file_name            text        NOT NULL CHECK (file_name ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.parquet$'),
    size_bytes           bigint      NOT NULL CHECK (size_bytes > 0),
    metadata_snapshot_id uuid        REFERENCES product_metadata_snapshots (id) ON DELETE RESTRICT,
    engine_version       text        NOT NULL CHECK (char_length(engine_version) <= 32),
    created_at           timestamptz NOT NULL,
    CONSTRAINT dataset_snapshots_range CHECK (range_end > range_start),
    CONSTRAINT dataset_snapshots_identity UNIQUE
        (product_uuid, granularity, range_start, range_end, max_ingest_run_id, content_sha256)
);

-- ------------------------------------------------------------------ backtests and reports
CREATE TABLE backtest_runs (
    id             uuid        PRIMARY KEY,
    snapshot_id    uuid        NOT NULL REFERENCES dataset_snapshots (id) ON DELETE RESTRICT,
    kind           text        NOT NULL CHECK (kind IN ('BACKTEST', 'WALK_FORWARD')),
    fee_scenario   text        NOT NULL CHECK (fee_scenario IN ('OPERATOR', 'STRESS')),
    config_sha256  text        NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    engine_version text        NOT NULL CHECK (char_length(engine_version) <= 32),
    params         jsonb       NOT NULL CHECK (octet_length(params::text) <= 65536),
    summary        jsonb       NOT NULL CHECK (octet_length(summary::text) <= 1048576),
    created_at     timestamptz NOT NULL,
    CONSTRAINT backtest_runs_identity UNIQUE
        (snapshot_id, kind, fee_scenario, config_sha256, engine_version)
);

CREATE TABLE reports (
    id         uuid        PRIMARY KEY,
    kind       text        NOT NULL CHECK (kind IN ('BACKTEST', 'WALK_FORWARD', 'PAPER_DAILY', 'DATA_QUALITY')),
    mode_label text        NOT NULL CHECK (mode_label IN ('BACKTEST', 'PAPER')),
    source_id  uuid,
    title      text        NOT NULL CHECK (char_length(title) <= 120),
    body_json  text        NOT NULL CHECK (octet_length(body_json) <= 2097152),
    body_md    text        NOT NULL CHECK (octet_length(body_md) <= 1048576),
    sha256     text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    CONSTRAINT reports_identity UNIQUE (kind, sha256),
    CONSTRAINT reports_mode_matches_kind CHECK (
        (kind IN ('BACKTEST', 'WALK_FORWARD', 'DATA_QUALITY') AND mode_label = 'BACKTEST')
        OR (kind = 'PAPER_DAILY' AND mode_label = 'PAPER'))
);
CREATE INDEX reports_recent_idx ON reports (created_at DESC, id);

CREATE TABLE grid_plans (
    id            uuid        PRIMARY KEY,
    pair_id       uuid        NOT NULL REFERENCES pairs (id) ON DELETE RESTRICT,
    levels        integer     NOT NULL CHECK (levels BETWEEN 3 AND 5),
    lower_price   numeric     NOT NULL CHECK (lower_price > 0),
    upper_price   numeric     NOT NULL CHECK (upper_price > lower_price),
    prices        numeric[]   NOT NULL CHECK (cardinality(prices) BETWEEN 3 AND 5),
    cell_budget   numeric     NOT NULL CHECK (cell_budget > 0 AND cell_budget <= 35),
    config_sha256 text        NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    details       jsonb       NOT NULL CHECK (octet_length(details::text) <= 65536),
    created_at    timestamptz NOT NULL,
    CONSTRAINT grid_plans_levels_match CHECK (cardinality(prices) = levels)
);

-- ------------------------------------------------------------------ paper exchange (local, PAPER only)
CREATE TABLE paper_session (
    id               boolean     PRIMARY KEY DEFAULT true CHECK (id),
    state            text        NOT NULL CHECK (state IN ('PAUSED', 'RUNNING')),
    pair_id          uuid        REFERENCES pairs (id) ON DELETE RESTRICT,
    grid_plan_id     uuid        REFERENCES grid_plans (id) ON DELETE RESTRICT,
    last_candle_start bigint     CHECK (last_candle_start IS NULL OR last_candle_start % 300 = 0),
    phase            text        NOT NULL DEFAULT 'IDLE' CHECK (phase IN ('IDLE', 'ACTIVE', 'STOPPED', 'HALTED')),
    peak_equity      numeric     NOT NULL DEFAULT 50 CHECK (peak_equity > 0),
    updated_at       timestamptz NOT NULL,
    CONSTRAINT paper_running_has_pair CHECK (state <> 'RUNNING' OR pair_id IS NOT NULL)
);
INSERT INTO paper_session (id, state, updated_at) VALUES (true, 'PAUSED', now());

CREATE TABLE paper_orders (
    id              uuid        PRIMARY KEY,
    venue           text        NOT NULL CHECK (venue = 'PAPER'),
    pair_id         uuid        NOT NULL REFERENCES pairs (id) ON DELETE RESTRICT,
    grid_plan_id    uuid        NOT NULL REFERENCES grid_plans (id) ON DELETE RESTRICT,
    client_order_id uuid        NOT NULL,
    seq             integer     NOT NULL CHECK (seq >= 1),
    level_index     integer     NOT NULL CHECK (level_index BETWEEN 0 AND 4),
    cycle_no        integer     NOT NULL CHECK (cycle_no >= 0),
    side            text        NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price           numeric     NOT NULL CHECK (price > 0),
    base_qty        numeric     NOT NULL CHECK (base_qty > 0),
    filled_qty      numeric     NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
    quote_reserved  numeric     NOT NULL DEFAULT 0 CHECK (quote_reserved >= 0),
    order_type      text        NOT NULL CHECK (order_type = 'limit_limit_gtc'),
    post_only       boolean     NOT NULL CHECK (post_only),
    state           text        NOT NULL CHECK (state IN ('OPEN', 'FILLED', 'CANCELLED', 'REJECTED')),
    placed_candle   bigint      NOT NULL,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    CONSTRAINT paper_orders_client_unique UNIQUE (client_order_id),
    CONSTRAINT paper_orders_seq_unique UNIQUE (grid_plan_id, seq),
    CONSTRAINT paper_orders_fill_bound CHECK (filled_qty <= base_qty),
    CONSTRAINT paper_orders_filled_state CHECK (state <> 'FILLED' OR filled_qty = base_qty),
    CONSTRAINT paper_orders_sell_reserves_nothing CHECK (side = 'BUY' OR quote_reserved = 0),
    CONSTRAINT paper_orders_closed_reserves_nothing CHECK (state = 'OPEN' OR quote_reserved = 0)
);
CREATE INDEX paper_orders_open_idx ON paper_orders (pair_id) WHERE state = 'OPEN';

CREATE TABLE paper_fills (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     uuid        NOT NULL REFERENCES paper_orders (id) ON DELETE RESTRICT,
    candle_start bigint      NOT NULL,
    price        numeric     NOT NULL CHECK (price > 0),
    base_qty     numeric     NOT NULL CHECK (base_qty > 0),
    notional     numeric     NOT NULL CHECK (notional > 0),
    fee          numeric     NOT NULL CHECK (fee >= 0),
    liquidity    text        NOT NULL CHECK (liquidity = 'MAKER'),
    CONSTRAINT paper_fills_once_per_candle UNIQUE (order_id, candle_start)
);

CREATE TABLE paper_ledger_entries (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_key   text        NOT NULL CHECK (char_length(entry_key) BETWEEN 3 AND 100),
    kind        text        NOT NULL CHECK (kind IN ('DEPOSIT', 'FILL_BUY', 'FILL_SELL', 'FEE')),
    quote_delta numeric     NOT NULL,
    base_delta  numeric     NOT NULL DEFAULT 0,
    order_id    uuid        REFERENCES paper_orders (id) ON DELETE RESTRICT,
    occurred_at timestamptz NOT NULL,
    CONSTRAINT paper_ledger_entry_key_unique UNIQUE (entry_key),
    CONSTRAINT paper_ledger_deposit_is_the_policy_capital CHECK (kind <> 'DEPOSIT' OR (quote_delta = 50 AND base_delta = 0))
);
CREATE UNIQUE INDEX paper_ledger_single_deposit ON paper_ledger_entries ((true)) WHERE kind = 'DEPOSIT';

CREATE TABLE paper_positions (
    pair_id    uuid        PRIMARY KEY REFERENCES pairs (id) ON DELETE RESTRICT,
    base_qty   numeric     NOT NULL CHECK (base_qty >= 0),
    cost_basis numeric     NOT NULL CHECK (cost_basis >= 0),
    updated_at timestamptz NOT NULL,
    CONSTRAINT paper_positions_empty_has_no_cost CHECK (base_qty > 0 OR cost_basis = 0)
);

-- ------------------------------------------------------------------ append-only / immutable
DO $do$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['ingest_runs', 'candles', 'candle_conflicts', 'data_quality_events',
                             'dataset_snapshots', 'backtest_runs', 'reports', 'grid_plans',
                             'paper_fills', 'paper_ledger_entries']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION td_append_only()', t || '_append_only', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION td_append_only()', t || '_no_truncate', t);
    END LOOP;
END
$do$;

-- The ingestion cursor only moves forward.
CREATE FUNCTION ingest_cursor_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS
$fn$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ingest_cursors rows are never deleted' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.covered_until <= OLD.covered_until THEN
        RAISE EXCEPTION 'the ingestion cursor only moves forward' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER ingest_cursors_forward_only
    BEFORE UPDATE OR DELETE ON ingest_cursors FOR EACH ROW EXECUTE FUNCTION ingest_cursor_guard();

-- Paper orders exist only for the one PAPER_ACTIVE pair, and the paper session only points at it.
CREATE FUNCTION paper_order_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (SELECT 1 FROM pairs p WHERE p.id = NEW.pair_id AND p.state = 'PAPER_ACTIVE') THEN
            RAISE EXCEPTION 'paper orders exist only for the PAPER_ACTIVE pair' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF (SELECT state FROM paper_session WHERE id) <> 'RUNNING' THEN
            RAISE EXCEPTION 'paper orders need a RUNNING paper session' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id <> OLD.id OR NEW.pair_id <> OLD.pair_id OR NEW.side <> OLD.side OR NEW.price <> OLD.price
       OR NEW.base_qty <> OLD.base_qty OR NEW.client_order_id <> OLD.client_order_id OR NEW.seq <> OLD.seq
       OR NEW.level_index <> OLD.level_index OR NEW.cycle_no <> OLD.cycle_no
       OR NEW.grid_plan_id <> OLD.grid_plan_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'paper order terms are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state <> 'OPEN' THEN
        RAISE EXCEPTION 'a closed paper order cannot change' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.filled_qty < OLD.filled_qty THEN
        RAISE EXCEPTION 'filled quantity cannot decrease' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER paper_orders_guard BEFORE INSERT OR UPDATE ON paper_orders
    FOR EACH ROW EXECUTE FUNCTION paper_order_guard();
CREATE TRIGGER paper_orders_no_delete BEFORE DELETE OR TRUNCATE ON paper_orders
    FOR EACH STATEMENT EXECUTE FUNCTION td_append_only();

-- Capital ceilings as a database backstop (hard constants: 50 total, 15 protected reserve, 35 deployed).
--   free cash          = cash - quote reserved by open BUY orders   must stay >= 15
--   deployed           = reserved by open BUY orders + inventory cost must stay <= 35
CREATE FUNCTION paper_capital_check() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
DECLARE
    cash numeric;
    reserved numeric;
    cost numeric;
BEGIN
    SELECT COALESCE(sum(quote_delta), 0) INTO cash FROM paper_ledger_entries;
    SELECT COALESCE(sum(quote_reserved), 0) INTO reserved FROM paper_orders WHERE state = 'OPEN';
    SELECT COALESCE(sum(cost_basis), 0) INTO cost FROM paper_positions;
    IF cash > 0 AND cash - reserved < 15 THEN
        RAISE EXCEPTION 'paper reserve breached: free cash below 15' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF reserved + cost > 35 THEN
        RAISE EXCEPTION 'paper deployment cap breached: above 35' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END
$fn$;
CREATE CONSTRAINT TRIGGER paper_orders_capital AFTER INSERT OR UPDATE ON paper_orders
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION paper_capital_check();
CREATE CONSTRAINT TRIGGER paper_positions_capital AFTER INSERT OR UPDATE ON paper_positions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION paper_capital_check();
CREATE CONSTRAINT TRIGGER paper_ledger_capital AFTER INSERT ON paper_ledger_entries
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION paper_capital_check();

-- ------------------------------------------------------------------ grants
GRANT SELECT ON ingest_runs, candles, candle_conflicts, data_quality_events, ingest_cursors,
    dataset_snapshots, backtest_runs, reports, grid_plans, paper_session, paper_orders,
    paper_fills, paper_ledger_entries, paper_positions TO td_app, td_ctl;

GRANT INSERT ON ingest_runs, candles, candle_conflicts, data_quality_events, dataset_snapshots,
    backtest_runs, reports, grid_plans, paper_orders, paper_fills, paper_ledger_entries,
    paper_positions TO td_ctl;
GRANT INSERT ON ingest_cursors TO td_ctl;
GRANT UPDATE (covered_until, last_run_id, updated_at) ON ingest_cursors TO td_ctl;
GRANT UPDATE (state, filled_qty, quote_reserved, updated_at) ON paper_orders TO td_ctl;
GRANT UPDATE (base_qty, cost_basis, updated_at) ON paper_positions TO td_ctl;
GRANT UPDATE (state, pair_id, grid_plan_id, last_candle_start, phase, peak_equity, updated_at) ON paper_session TO td_ctl;
