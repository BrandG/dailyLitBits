"""SQLite access for dailyLitBits.

One database file, held on the host at <project>/data/app.db and mounted into
the container at /data. WAL mode so the web app can keep reading while a cron
script writes; writes here are a handful of rows per email, so a single
writer at a time is not a constraint worth engineering around.

This replaces the MongoDB that belonged to the BlueHorseshoe stack. Keeping
our own file means a config change over there can no longer take this site
down, and a backup is one copyable, restorable file.
"""
import os
import sqlite3
from contextlib import contextmanager

import config

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect(db_path=None):
    """Opens a connection with the pragmas this app needs."""
    path = db_path or config.DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # isolation_level=None means autocommit; use transaction() below to batch.
    #
    # check_same_thread=False because FastAPI opens the connection in a
    # worker thread (the dependency) and uses it on the event loop thread
    # (the route). That is safe here only because a connection belongs to
    # exactly one request and is used serially within it - never share one
    # across concurrent work.
    conn = sqlite3.connect(path, timeout=15.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def add_missing_columns(conn):
    """The one schema change CREATE TABLE IF NOT EXISTS cannot make.

    schema.sql describes the table a fresh install gets. A database that
    already exists keeps the columns it already had, so a new one has to be
    added here, guarded by what the table actually holds. Every branch is a
    no-op the second time, which is what lets init_db run on every connection.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")}

    if "queue_position" not in columns:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN queue_position INTEGER")
        # Existing queues keep the order they were already being shown in,
        # which was the order the books were added.
        conn.execute(
            """UPDATE subscriptions
               SET queue_position = (
                   SELECT COUNT(*) FROM subscriptions AS earlier
                   WHERE earlier.user_id = subscriptions.user_id
                     AND earlier.status = 'queued'
                     AND (earlier.created_at, earlier.id)
                         <= (subscriptions.created_at, subscriptions.id))
               WHERE status = 'queued'"""
        )

    return conn


def init_db(conn):
    """Applies schema.sql, then the changes it cannot make. Idempotent, so it
    is safe on every connection."""
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    add_missing_columns(conn)
    return conn


def get_db(db_path=None):
    """The normal entry point: an initialised connection."""
    return init_db(connect(db_path))


@contextmanager
def transaction(conn):
    """Wraps a batch of writes so a failure leaves nothing half-applied."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
