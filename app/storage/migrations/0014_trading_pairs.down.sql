DROP TRIGGER IF EXISTS trading_config_pairs_fit_trigger ON trading_config;
DROP TABLE IF EXISTS trading_pairs;
DROP FUNCTION IF EXISTS trading_pairs_fit();
DROP FUNCTION IF EXISTS trading_pairs_guard();
