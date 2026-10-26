"""The delivery engine: how much goes out, which recap, and when."""
from datetime import datetime, timedelta, timezone

import dispatch
import security


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def test_a_delivery_is_chunks_per_day_chunks(conn, reader, sent):
    _, sub_id = reader
    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn)

    assert ok, message
    assert len(sent) == 1
    # Two chunks per day, starting at 1, so parts 1 and 2 in one email.
    assert "Parts 1-2 of 10" in sent[0]["subject"]
    assert "the text of part 1" in sent[0]["body"]
    assert "the text of part 2" in sent[0]["body"]
    assert "the text of part 3" not in sent[0]["body"]

    row = conn.execute("SELECT current_sequence, last_sent FROM subscriptions WHERE id = ?",
                       (sub_id,)).fetchone()
    assert row["current_sequence"] == 3
    assert row["last_sent"] is not None


def test_one_chunk_a_day_reads_part_not_parts(conn, reader, sent):
    _, sub_id = reader
    conn.execute("UPDATE subscriptions SET chunks_per_day = 1 WHERE id = ?", (sub_id,))

    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok
    assert "Part 1 of 10" in sent[0]["subject"]


def test_recap_comes_from_the_chunk_before_the_delivery(conn, reader, sent):
    _, sub_id = reader
    conn.execute(
        "UPDATE subscriptions SET current_sequence = 5, chunks_per_day = 3 WHERE id = ?",
        (sub_id,))

    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok
    # Starting at 5 means the story so far is chunk 4's recap, whatever N is.
    assert "Recap through part 4." in sent[0]["body"]
    assert "Recap through part 5." not in sent[0]["body"]


def test_first_delivery_has_no_recap(conn, reader, sent):
    _, sub_id = reader
    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok
    assert "Previously:" not in sent[0]["body"]


def test_delivery_size_change_does_not_move_the_readers_place(conn, reader, sent):
    """The whole reason the three-edition scheme went away."""
    _, sub_id = reader
    conn.execute("UPDATE subscriptions SET current_sequence = 7 WHERE id = ?", (sub_id,))
    conn.execute("UPDATE subscriptions SET chunks_per_day = 4 WHERE id = ?", (sub_id,))

    row = conn.execute("SELECT current_sequence FROM subscriptions WHERE id = ?",
                       (sub_id,)).fetchone()
    assert row["current_sequence"] == 7

    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok
    assert "Parts 7-10 of 10" in sent[0]["subject"]


def test_a_short_last_delivery_is_fine(conn, reader, sent):
    _, sub_id = reader
    conn.execute(
        "UPDATE subscriptions SET current_sequence = 9, chunks_per_day = 4 WHERE id = ?",
        (sub_id,))

    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok
    assert "Parts 9-10 of 10" in sent[0]["subject"]
    assert conn.execute("SELECT current_sequence FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()["current_sequence"] == 11


def test_running_off_the_end_sends_the_victory_email(conn, reader, sent, monkeypatch):
    monkeypatch.setattr(dispatch.ai, "get_recommendations", lambda *a, **k: [])
    _, sub_id = reader
    conn.execute("UPDATE subscriptions SET current_sequence = 11 WHERE id = ?", (sub_id,))

    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok, message
    assert "finished" in sent[0]["subject"]
    assert conn.execute("SELECT status FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()["status"] == "completed"


def test_finishing_promotes_the_queued_book(conn, reader, sent, monkeypatch):
    monkeypatch.setattr(dispatch.ai, "get_recommendations", lambda *a, **k: [])
    user_id, sub_id = reader

    conn.execute(
        """INSERT INTO books (id, title, author, total_chunks, total_words, created_at)
           VALUES ('pgnext', 'Next Book', 'Someone', 3, 900, '2026-01-01T00:00:00+00:00')""")
    conn.execute(
        """INSERT INTO subscriptions
               (user_id, book_id, status, current_sequence, chunks_per_day,
                delivery_hour, created_at)
           VALUES (?, 'pgnext', 'queued', 1, 2, 6, '2026-01-02T00:00:00+00:00')""",
        (user_id,))
    conn.execute("UPDATE subscriptions SET current_sequence = 11 WHERE id = ?", (sub_id,))

    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok
    assert conn.execute(
        "SELECT status FROM subscriptions WHERE book_id = 'pgnext'").fetchone()["status"] == "active"
    assert "Next Book" in sent[0]["body"]


def test_dry_run_sends_nothing(conn, reader, sent):
    _, sub_id = reader
    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn, dry_run=True)

    assert ok
    assert "[dry run]" in message
    assert sent == []
    assert conn.execute("SELECT current_sequence FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()["current_sequence"] == 1


def test_next_link_is_rate_limited(conn, reader, sent):
    _, sub_id = reader
    conn.execute("UPDATE subscriptions SET last_sent = ? WHERE id = ?", (utc_now(), sub_id))

    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert not ok
    assert "wait" in message.lower()
    assert sent == []


def test_cron_only_sends_in_the_readers_delivery_hour(conn, reader, sent):
    _, sub_id = reader
    this_hour = datetime.now(timezone.utc).hour

    conn.execute("UPDATE subscriptions SET delivery_hour = ? WHERE id = ?",
                 ((this_hour + 1) % 24, sub_id))
    ok, message = dispatch.process_subscription(sub_id, trigger="cron", conn=conn)
    assert not ok and "Not delivery time" in message

    conn.execute("UPDATE subscriptions SET delivery_hour = ? WHERE id = ?", (this_hour, sub_id))
    ok, _ = dispatch.process_subscription(sub_id, trigger="cron", conn=conn)
    assert ok
    assert len(sent) == 1


def test_cron_will_not_send_twice_in_a_day(conn, reader, sent):
    _, sub_id = reader
    this_hour = datetime.now(timezone.utc).hour
    conn.execute("UPDATE subscriptions SET delivery_hour = ?, last_sent = ? WHERE id = ?",
                 (this_hour, utc_now(), sub_id))

    ok, message = dispatch.process_subscription(sub_id, trigger="cron", conn=conn)
    assert not ok
    assert "Already sent today" in message


def test_paused_subscriptions_are_left_alone(conn, reader, sent):
    _, sub_id = reader
    conn.execute("UPDATE subscriptions SET status = 'paused' WHERE id = ?", (sub_id,))

    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert not ok
    assert "paused" in message
    assert sent == []


def test_the_email_carries_working_links(conn, reader, sent):
    user_id, sub_id = reader
    assert dispatch.process_subscription(sub_id, trigger="next", conn=conn)[0]

    body = sent[0]["body"]
    assert f"/unsubscribe?token={security.generate_unsub_token(sub_id)}" in body
    assert f"/next?token={security.generate_next_token(sub_id)}" in body
    assert f"/profile?token={security.generate_dashboard_token(user_id)}" in body
