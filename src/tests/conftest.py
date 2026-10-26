"""Test fixtures.

Each test module gets its own SQLite file in a temp directory, so tests no
longer need a live database they have permission to create. DB_PATH is set
before the app modules are imported, because config reads it at import time.

Nothing here sends email: send_via_brevo is replaced with a recorder, and the
tests assert against what would have been sent.
"""
import os
import sys
import tempfile

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="dlb-test-"), "test.db")

import db  # noqa: E402
import dispatch  # noqa: E402
import main  # noqa: E402
import security  # noqa: E402
from user_manager import UserManager  # noqa: E402

BOOK_ID = "pgtest"
CHUNK_COUNT = 10
WORDS_PER_CHUNK = 320


@pytest.fixture
def conn():
    """A clean database for every test."""
    connection = db.get_db()
    # events first: it references subscriptions, and rows left behind would
    # leak between tests that assert on the exact contents of the log.
    for table in ("events", "chunks", "subscriptions", "books", "users", "suggestions"):
        connection.execute(f"DELETE FROM {table}")
    yield connection
    connection.close()


@pytest.fixture
def book(conn):
    """One book, ten chunks, recaps on all but the last."""
    conn.execute(
        """INSERT INTO books (id, title, author, blurb, total_chunks, total_words, created_at)
           VALUES (?, 'Test Book', 'A Test Author', 'A blurb.', ?, ?, '2026-01-01T00:00:00+00:00')""",
        (BOOK_ID, CHUNK_COUNT, CHUNK_COUNT * WORDS_PER_CHUNK),
    )
    conn.executemany(
        "INSERT INTO chunks (book_id, sequence, content, word_count, recap) VALUES (?, ?, ?, ?, ?)",
        [
            (BOOK_ID, n, f"This is the text of part {n}. " * 20, WORDS_PER_CHUNK,
             f"Recap through part {n}." if n < CHUNK_COUNT else None)
            for n in range(1, CHUNK_COUNT + 1)
        ],
    )
    return BOOK_ID


@pytest.fixture
def sent(monkeypatch):
    """Captures outgoing email instead of sending it."""
    outbox = []

    def fake_send(to_email, subject, html_body):
        outbox.append({"to": to_email, "subject": subject, "body": html_body})
        return True

    monkeypatch.setattr(dispatch, "send_via_brevo", fake_send)
    return outbox


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(main.app)


@pytest.fixture
def reader(conn, book):
    """A subscriber part-way through the book. Returns (user_id, sub_id)."""
    user_id, _ = UserManager(conn).get_or_create_user("reader@example.com")
    cur = conn.execute(
        """INSERT INTO subscriptions
               (user_id, book_id, status, current_sequence, chunks_per_day,
                delivery_hour, last_sent, created_at)
           VALUES (?, ?, 'active', 1, 2, 6, NULL, '2026-01-01T00:00:00+00:00')""",
        (user_id, book),
    )
    return user_id, cur.lastrowid
