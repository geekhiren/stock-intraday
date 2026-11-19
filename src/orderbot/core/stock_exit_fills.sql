CREATE TABLE IF NOT EXISTS stock_exit_fills (
    trade_id INTEGER NOT NULL REFERENCES stock_trades(trade_id),
    quote_at_utc TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    value_paise INTEGER NOT NULL CHECK (value_paise > 0),
    reason TEXT NOT NULL,
    PRIMARY KEY (trade_id, quote_at_utc)
);
