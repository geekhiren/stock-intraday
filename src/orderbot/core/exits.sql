CREATE TABLE IF NOT EXISTS kill_switch_latches (
    session_day TEXT PRIMARY KEY NOT NULL,
    at_utc TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exit_progress (
    decision_id TEXT PRIMARY KEY NOT NULL REFERENCES decisions(decision_id),
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exit_audit (
    event_id INTEGER PRIMARY KEY,
    at_utc TEXT NOT NULL,
    event TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    once_key TEXT UNIQUE
);
