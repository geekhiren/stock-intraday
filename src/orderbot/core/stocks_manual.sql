-- Rebuild stock_trades without the strategy CHECK so manual paper trades can be stored.
CREATE TABLE stock_trades_v2 (
    trade_id INTEGER PRIMARY KEY,
    session_day TEXT NOT NULL,
    token TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    entry_at_utc TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    stop TEXT NOT NULL,
    target TEXT NOT NULL,
    exit_at_utc TEXT,
    exit_price TEXT,
    exit_reason TEXT,
    charges_paise INTEGER,
    net_paise INTEGER
);
INSERT INTO stock_trades_v2 SELECT * FROM stock_trades;
DROP TABLE stock_trades;
ALTER TABLE stock_trades_v2 RENAME TO stock_trades;
CREATE INDEX IF NOT EXISTS stock_trades_day ON stock_trades(session_day, trade_id);
