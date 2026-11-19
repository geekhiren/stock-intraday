-- Stocks-in-play candidates frozen after the opening range. A row with an empty token marks the day as evaluated.
CREATE TABLE IF NOT EXISTS stock_plays (
    session_day TEXT NOT NULL,
    token TEXT NOT NULL,
    side TEXT NOT NULL,
    edge TEXT NOT NULL,
    stop_distance TEXT NOT NULL,
    relative_volume REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('waiting', 'done')),
    PRIMARY KEY (session_day, token)
);
