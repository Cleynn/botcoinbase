-- Coinbase account data for the web interface. The host feed reads the exchange with GET requests
-- only and replaces these rows; the web role may only read them. Display data: nothing in the
-- order path, the risk rules or reconciliation reads these tables.
CREATE TABLE exchange_feed (
    venue          text        PRIMARY KEY CHECK (venue = 'COINBASE'),
    state          text        NOT NULL CHECK (state IN ('OK', 'FAILED')),
    error_code     text        CHECK (error_code IS NULL OR error_code ~ '^[A-Z0-9_]{3,40}$'),
    attempted_at   timestamptz NOT NULL,
    succeeded_at   timestamptz,
    can_view       boolean,
    can_trade      boolean,
    can_transfer   boolean,
    portfolio_type text        CHECK (portfolio_type IS NULL OR portfolio_type ~ '^[A-Z_]{1,40}$'),
    CONSTRAINT exchange_feed_state_consistent CHECK ((state = 'OK') = (error_code IS NULL)),
    CONSTRAINT exchange_feed_ok_has_success CHECK (state <> 'OK' OR succeeded_at = attempted_at)
);

CREATE TABLE exchange_feed_balances (
    venue     text    NOT NULL REFERENCES exchange_feed (venue) ON DELETE CASCADE,
    currency  text    NOT NULL CHECK (currency ~ '^[A-Z0-9]{1,20}$'),
    available numeric NOT NULL CHECK (available >= 0),
    hold      numeric NOT NULL CHECK (hold >= 0),
    PRIMARY KEY (venue, currency)
);

CREATE TABLE exchange_feed_orders (
    venue        text    NOT NULL REFERENCES exchange_feed (venue) ON DELETE CASCADE,
    order_id     text    NOT NULL CHECK (order_id ~ '^[A-Za-z0-9-]{8,64}$'),
    product_id   text    NOT NULL CHECK (product_id ~ '^[A-Z0-9]+-[A-Z0-9]+$' AND char_length(product_id) <= 41),
    side         text    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    status       text    NOT NULL CHECK (status ~ '^[A-Z_]{3,30}$'),
    price        numeric NOT NULL CHECK (price >= 0),
    base_qty     numeric NOT NULL CHECK (base_qty >= 0),
    filled_qty   numeric NOT NULL CHECK (filled_qty >= 0),
    created_time timestamptz,
    PRIMARY KEY (venue, order_id)
);

CREATE TABLE exchange_feed_fills (
    venue      text    NOT NULL REFERENCES exchange_feed (venue) ON DELETE CASCADE,
    fill_id    text    NOT NULL CHECK (char_length(fill_id) BETWEEN 1 AND 100),
    order_id   text    NOT NULL CHECK (char_length(order_id) BETWEEN 1 AND 100),
    product_id text    NOT NULL CHECK (product_id ~ '^[A-Z0-9]+-[A-Z0-9]+$' AND char_length(product_id) <= 41),
    side       text    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price      numeric NOT NULL CHECK (price >= 0),
    size       numeric NOT NULL CHECK (size >= 0),
    fee        numeric NOT NULL CHECK (fee >= 0),
    liquidity  text    NOT NULL CHECK (liquidity IN ('MAKER', 'TAKER', 'UNKNOWN')),
    trade_time timestamptz,
    PRIMARY KEY (venue, fill_id)
);

-- Only the host writes, and the recorded times come from the database clock.
CREATE FUNCTION exchange_feed_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$fn$
BEGIN
    IF td_actor_class() IS DISTINCT FROM 'HOST' THEN
        RAISE EXCEPTION 'only the host writes the exchange feed' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME = 'exchange_feed' AND TG_OP <> 'DELETE' THEN
        PERFORM td_check_time(NEW.attempted_at);
    END IF;
    RETURN COALESCE(NEW, OLD);
END
$fn$;
CREATE TRIGGER exchange_feed_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON exchange_feed
    FOR EACH ROW EXECUTE FUNCTION exchange_feed_guard();
CREATE TRIGGER exchange_feed_balances_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON exchange_feed_balances
    FOR EACH ROW EXECUTE FUNCTION exchange_feed_guard();
CREATE TRIGGER exchange_feed_orders_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON exchange_feed_orders
    FOR EACH ROW EXECUTE FUNCTION exchange_feed_guard();
CREATE TRIGGER exchange_feed_fills_guard_trigger BEFORE INSERT OR UPDATE OR DELETE ON exchange_feed_fills
    FOR EACH ROW EXECUTE FUNCTION exchange_feed_guard();

GRANT SELECT ON exchange_feed, exchange_feed_balances, exchange_feed_orders, exchange_feed_fills TO td_app, td_ctl;
GRANT INSERT, UPDATE ON exchange_feed TO td_ctl;
GRANT INSERT, DELETE ON exchange_feed_balances, exchange_feed_orders, exchange_feed_fills TO td_ctl;
