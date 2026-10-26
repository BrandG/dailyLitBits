"""Subscribe someone to a book from the command line.

The website does the same thing through /signup; this is for adding a reader
by hand and for testing a delivery end to end.
"""
import argparse
from datetime import datetime, timezone

import db
import dispatch
from user_manager import UserManager


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def subscribe_user(email, book_id, chunks_per_day=2, delivery_hour=None, conn=None):
    """Returns (ok, message, subscription_id)."""
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_db()

    if delivery_hour is None:
        delivery_hour = dispatch.TARGET_HOUR

    try:
        book = conn.execute("SELECT id, title FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            return False, f"Book '{book_id}' is not in the library.", None

        manager = UserManager(conn)
        with db.transaction(conn):
            user_id, created = manager.get_or_create_user(email)

            existing = conn.execute(
                """SELECT id, status FROM subscriptions
                   WHERE user_id = ? AND book_id = ?
                     AND status IN ('active', 'queued', 'paused')""",
                (user_id, book_id),
            ).fetchone()
            if existing:
                return (False,
                        f"Already subscribed (status: {existing['status']}).",
                        existing["id"])

            active = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()[0]
            status = "active" if active == 0 else "queued"

            cur = conn.execute(
                """INSERT INTO subscriptions
                       (user_id, book_id, status, current_sequence, chunks_per_day,
                        delivery_hour, last_sent, created_at)
                   VALUES (?, ?, ?, 1, ?, ?, NULL, ?)""",
                (user_id, book_id, status, chunks_per_day, delivery_hour, utc_now()),
            )
            sub_id = cur.lastrowid

        who = "new reader" if created else "existing reader"
        return True, f"Subscribed ({who}) to '{book['title']}' as {status}.", sub_id
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Subscribe a reader to a book")
    parser.add_argument("email")
    parser.add_argument("book_id", help="Book id, e.g. pg84")
    parser.add_argument("--chunks-per-day", type=int, default=2)
    parser.add_argument("--delivery-hour", type=int)
    args = parser.parse_args()

    ok, message, sub_id = subscribe_user(
        args.email, args.book_id, args.chunks_per_day, args.delivery_hour)
    print(("OK: " if ok else "FAILED: ") + message + (f" (id {sub_id})" if sub_id else ""))
