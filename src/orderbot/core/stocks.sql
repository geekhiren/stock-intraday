CREATE TABLE IF NOT EXISTS stock_trades (
    trade_id INTEGER PRIMARY KEY,
    session_day TEXT NOT NULL,
    token TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('orb', 'vwap')),
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

CREATE INDEX IF NOT EXISTS stock_trades_day ON stock_trades(session_day, trade_id);

CREATE TABLE IF NOT EXISTS stock_ranges (
    session_day TEXT NOT NULL,
    token TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    broken INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_day, token)
);
