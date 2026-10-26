-- dailyLitBits schema (SQLite).
--
-- Replaces the MongoDB collections that lived in BlueHorseshoe's shared
-- mongod; see the Incident Log in CLAUDE.md for why that coupling had to go.
--
-- Books are chunked ONCE, at ingest.CHUNK_TARGET_WORDS. How much a reader
-- receives per day is a property of their subscription, not of the stored
-- book, which is why there are no longer three editions of everything.
--
-- db.init_db() applies this on every connection, so every statement here
-- must stay idempotent.

PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    -- Fernet ciphertext. Non-deterministic, so it cannot be queried.
    email_enc    BLOB    NOT NULL,
    -- Keyed hash of the normalised address. Lets us find a user by email
    -- with one indexed lookup instead of decrypting every row, without
    -- storing the address in the clear.
    email_lookup TEXT    NOT NULL UNIQUE,
    timezone     TEXT    NOT NULL DEFAULT 'UTC',
    role         TEXT    NOT NULL DEFAULT 'reader'
                 CHECK (role IN ('reader', 'admin')),
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id           TEXT    PRIMARY KEY,          -- 'pg11'
    title        TEXT    NOT NULL,
    author       TEXT,
    blurb        TEXT,                          -- was books.description in Mongo
    cover_path   TEXT,
    source_url   TEXT,
    total_chunks INTEGER NOT NULL DEFAULT 0,
    total_words  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    book_id    TEXT    NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    sequence   INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    word_count INTEGER NOT NULL,
    -- Running 'story so far' through this chunk, generated once at ingest.
    -- A delivery starting at chunk S reads chunk S-1's recap, which works
    -- whatever chunks_per_day happens to be. NULL until generated.
    recap      TEXT,
    PRIMARY KEY (book_id, sequence)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id          TEXT    NOT NULL REFERENCES books(id),
    status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'queued', 'paused',
                                       'completed', 'unsubscribed')),
    current_sequence INTEGER NOT NULL DEFAULT 1,
    -- How many 325-word chunks arrive per delivery. 2 is roughly the old
    -- 750-word 'standard' edition.
    chunks_per_day   INTEGER NOT NULL DEFAULT 2
                     CHECK (chunks_per_day BETWEEN 1 AND 12),
    delivery_hour    INTEGER NOT NULL DEFAULT 6
                     CHECK (delivery_hour BETWEEN 0 AND 23),
    last_sent        TEXT,
    created_at       TEXT    NOT NULL
);

-- The indexes Mongo never had: the hourly dispatch asks for exactly this.
CREATE INDEX IF NOT EXISTS idx_subs_due  ON subscriptions(status, delivery_hour);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id, status);

CREATE TABLE IF NOT EXISTS suggestions (
    id           INTEGER PRIMARY KEY,
    title        TEXT    NOT NULL,
    author       TEXT,
    link         TEXT,
    comments     TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected')),
    submitted_at TEXT    NOT NULL
);
