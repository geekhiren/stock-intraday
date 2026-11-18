CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY NOT NULL,
    at_utc TEXT NOT NULL CHECK (length(at_utc) = 32 AND at_utc LIKE '%+00:00' AND julianday(at_utc) IS NOT NULL),
    strategy TEXT NOT NULL CHECK (strategy IN ('vwap', 'ohl')),
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS orders (
    order_tag TEXT PRIMARY KEY NOT NULL,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    intent_at_utc TEXT NOT NULL CHECK (length(intent_at_utc) = 32 AND intent_at_utc LIKE '%+00:00' AND julianday(intent_at_utc) IS NOT NULL),
    instrument_token TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    requested_quantity INTEGER NOT NULL CHECK (requested_quantity > 0),
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS order_events (
    event_id INTEGER PRIMARY KEY,
    order_tag TEXT NOT NULL REFERENCES orders(order_tag),
    at_utc TEXT NOT NULL CHECK (length(at_utc) = 32 AND at_utc LIKE '%+00:00' AND julianday(at_utc) IS NOT NULL),
    state TEXT NOT NULL CHECK (state IN ('INTENT', 'SENT', 'ACKED', 'PARTIAL', 'FILLED', 'REJECTED', 'CANCELLED')),
    filled_quantity INTEGER NOT NULL CHECK (filled_quantity >= 0),
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS order_events_by_tag ON order_events (order_tag, event_id);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY NOT NULL,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    entry_at_utc TEXT NOT NULL CHECK (length(entry_at_utc) = 32 AND entry_at_utc LIKE '%+00:00' AND julianday(entry_at_utc) IS NOT NULL),
    exit_at_utc TEXT NOT NULL CHECK (length(exit_at_utc) = 32 AND exit_at_utc LIKE '%+00:00' AND julianday(exit_at_utc) IS NOT NULL AND exit_at_utc >= entry_at_utc),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    net_pnl_paise INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
