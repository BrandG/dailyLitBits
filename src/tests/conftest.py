"""Test fixtures.

Each test module gets its own SQLite file in a temp directory, so tests no
longer need a live database they have permission to create. DB_PATH is set
before the app modules are imported, because config reads it at import time.

Nothing here sends email: send_via_brevo is replaced with a recorder, and the
tests assert against what would have been sent.
"""
import os
import re
import sys
import tempfile

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="dlb-test-"), "test.db")

import db  # noqa: E402
import dispatch  # noqa: E402
import main  # noqa: E402

# The real check, kept for the tests that exercise it; every other test runs
# with Turnstile passing (see service_open).
REAL_TURNSTILE_PASSES = main.turnstile_passes
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
    for table in ("events", "pending_signups", "suppressions", "login_requests", "chunks", "subscriptions", "books", "users",
                  "suggestions",
                  "acquisitions"):
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

    def fake_send(to_email, subject, html_body, headers=None):
        outbox.append({"to": to_email, "subject": subject, "body": html_body,
                       "headers": headers or {}})
        return True

    monkeypatch.setattr(dispatch, "send_via_brevo", fake_send)
    return outbox


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    # https, because the session cookie is Secure. Over plain http the client's
    # cookie jar silently drops it, and every session test would then pass or
    # fail for a reason that has nothing to do with the app.
    return TestClient(main.app, base_url="https://testserver")


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


@pytest.fixture(autouse=True)
def service_open(monkeypatch):
    """Tests create users 'now', which the containment hold would catch."""
    import config
    monkeypatch.setattr(config, "SIGNUPS_OPEN", True)
    monkeypatch.setattr(config, "DELIVERY_HOLD_SINCE", "")
    monkeypatch.setattr(main, "turnstile_passes", lambda token, ip: True)
    monkeypatch.setattr(config, "SIGNUPS_PER_IP_PER_HOUR", 1000)
    monkeypatch.setattr(config, "SIGNUPS_PER_IP_PER_DAY", 1000)


def confirm_token_from(email_body):
    match = re.search(r"/confirm\?token=([^\"&<\s]+)", email_body)
    assert match, "no confirmation link in the email"
    return match.group(1)


@pytest.fixture
def sign_up(client, sent):
    """The whole double opt-in, as a reader does it: the form, the one
    confirmation email, the button. Returns the confirm page's response."""
    def _sign_up(email, book_id, **form):
        before = len(sent)
        client.post("/signup", data={"email": email, "book_id": book_id, **form})
        assert len(sent) == before + 1, "signup should send exactly one confirmation"
        return client.post("/confirm", data={"token": confirm_token_from(sent[-1]["body"])})
    return _sign_up
