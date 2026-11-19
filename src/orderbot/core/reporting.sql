CREATE TABLE IF NOT EXISTS feed_status (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL CHECK (state IN ('connecting', 'connected', 'stale', 'reconnecting', 'disconnected', 'simulated')),
    observed_at_utc TEXT NOT NULL CHECK (
        length(observed_at_utc) = 32 AND observed_at_utc LIKE '%+00:00' AND julianday(observed_at_utc) IS NOT NULL
    )
);

CREATE TABLE IF NOT EXISTS alert_outbox (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    at_utc TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_utc TEXT NOT NULL,
    lease_until_utc TEXT,
    lease_id TEXT,
    sent_at_utc TEXT
);

CREATE INDEX IF NOT EXISTS alert_outbox_ready ON alert_outbox(next_attempt_utc, id) WHERE sent_at_utc IS NULL;
