CREATE TABLE IF NOT EXISTS atm_iv (
    session_day TEXT NOT NULL,
    option_right TEXT NOT NULL CHECK (option_right IN ('CE', 'PE')),
    observed_at_utc TEXT NOT NULL CHECK (
        length(observed_at_utc) = 32 AND observed_at_utc LIKE '%+00:00' AND julianday(observed_at_utc) IS NOT NULL
    ),
    iv TEXT NOT NULL,
    PRIMARY KEY (option_right, session_day)
);
