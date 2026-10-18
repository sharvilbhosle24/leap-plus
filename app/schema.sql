-- Leap+ application schema. Runs on every startup, so every statement is IF NOT EXISTS.
-- Money is always INTEGER cents, never REAL.
-- New columns on existing tables are added in db.py (SQLite has no ADD COLUMN IF NOT EXISTS).

-- One row per user. The balance is a SIMULATED Leap ledger, not a real Leap card.
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    email          TEXT    NOT NULL UNIQUE,
    balance_cents  INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Stage 1: the ledger. One row per credit. Only the Stripe webhook inserts here.
CREATE TABLE IF NOT EXISTS transactions (
    id                      INTEGER PRIMARY KEY,
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    type                    TEXT    NOT NULL CHECK (type IN ('topup', 'auto_topup')),
    amount_cents            INTEGER NOT NULL CHECK (amount_cents > 0),
    stripe_event_id         TEXT    NOT NULL UNIQUE,
    -- The Stripe payment being credited (Checkout Session cs_… or Invoice in_…).
    -- UNIQUE: one payment can never be credited twice, even by two different events.
    stripe_object_id        TEXT    NOT NULL UNIQUE,
    stripe_subscription_id  TEXT,
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Stage 1: every Stripe event we have processed. A replayed event id is ignored.
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id     TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    api_version  TEXT,
    outcome      TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Stage 1: Checkout Sessions we created, so the UI can ask "has this payment landed yet?".
CREATE TABLE IF NOT EXISTS checkout_sessions (
    id                      TEXT    PRIMARY KEY,
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    kind                    TEXT    NOT NULL CHECK (kind IN ('topup', 'auto_topup')),
    amount_cents            INTEGER NOT NULL CHECK (amount_cents > 0),
    stripe_subscription_id  TEXT,
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Stage 1: weekly auto-top-up subscriptions, mirrored from Stripe by the webhook.
CREATE TABLE IF NOT EXISTS subscriptions (
    id            TEXT    PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    status        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Stops imported from NTA static GTFS by scripts/import_stops.py. kind: bus / rail / tram.
CREATE TABLE IF NOT EXISTS stops (
    stop_id  TEXT PRIMARY KEY,
    code     TEXT,
    name     TEXT NOT NULL,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('bus', 'rail', 'tram'))
);
CREATE INDEX IF NOT EXISTS stops_kind ON stops(kind);

-- Stage 3: a user's favourite stops.
CREATE TABLE IF NOT EXISTS favourite_stops (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    stop_id     TEXT    NOT NULL REFERENCES stops(stop_id),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (user_id, stop_id)
);

-- Stage 2: timetable for rail, Luas and south Dublin buses (imported by scripts/import_stops.py).
-- Live times = scheduled time here + the delay from the NTA GTFS-Realtime TripUpdates feed.
CREATE TABLE IF NOT EXISTS routes (
    route_id    TEXT PRIMARY KEY,
    short_name  TEXT,
    long_name   TEXT,
    route_type  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trips (
    trip_id       TEXT PRIMARY KEY,
    route_id      TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    headsign      TEXT,
    direction_id  INTEGER,
    last_seq      INTEGER NOT NULL  -- the trip's final stop_sequence: no departures from there
);
CREATE TABLE IF NOT EXISTS stop_times (
    trip_id         TEXT    NOT NULL,
    stop_id         TEXT    NOT NULL,
    stop_sequence   INTEGER NOT NULL,
    departure_secs  INTEGER NOT NULL  -- seconds after midnight of the service day (can exceed 86400)
);
CREATE INDEX IF NOT EXISTS stop_times_stop ON stop_times(stop_id, departure_secs);
CREATE TABLE IF NOT EXISTS calendar (
    service_id  TEXT PRIMARY KEY,
    days        TEXT NOT NULL,  -- 7 chars, Monday first, e.g. '1111100'
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    exception_type  INTEGER NOT NULL  -- 1 = added, 2 = removed
);
CREATE INDEX IF NOT EXISTS stop_times_trip ON stop_times(trip_id, stop_sequence);
