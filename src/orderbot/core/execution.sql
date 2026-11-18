CREATE TABLE IF NOT EXISTS execution_halts (
    session_day TEXT PRIMARY KEY NOT NULL,
    at_utc TEXT NOT NULL CHECK (length(at_utc) = 32 AND at_utc LIKE '%+00:00' AND julianday(at_utc) IS NOT NULL),
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    tag TEXT PRIMARY KEY NOT NULL,
    intent_json TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_depth_usage (
    token TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    at_utc TEXT NOT NULL CHECK (length(at_utc) = 32 AND at_utc LIKE '%+00:00' AND julianday(at_utc) IS NOT NULL),
    touch_paise INTEGER NOT NULL,
    units INTEGER NOT NULL CHECK (units > 0),
    PRIMARY KEY (token, side, at_utc, touch_paise)
);
