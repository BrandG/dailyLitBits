"""The event log: that it records what happened, and nothing about who."""
import json

import pytest

import dispatch
import events
import security
from user_manager import UserManager


def rows(conn):
    return conn.execute("SELECT * FROM events ORDER BY id").fetchall()


# --- the guard rails ---

def test_an_unknown_event_name_is_refused(conn):
    with pytest.raises(ValueError, match="unknown event"):
        events.record(conn, "harvested_for_advertising")


def test_detail_keys_outside_the_allowlist_are_refused(conn):
    with pytest.raises(ValueError, match="not allowed"):
        events.record(conn, "paused", email="reader@example.com")
    with pytest.raises(ValueError, match="not allowed"):
        events.record(conn, "paused", ip_address="203.0.113.7")
    assert rows(conn) == []


def test_a_detail_value_shaped_like_an_address_is_refused(conn):
    with pytest.raises(ValueError, match="address"):
        events.record(conn, "paused", timezone="reader@example.com")


# --- what gets recorded ---

def test_signing_up_is_recorded(client, conn, book, sent):
    client.post("/signup", data={"email": "new@example.com", "book_id": book})

    logged = rows(conn)
    assert [row["event"] for row in logged] == ["subscribed"]
    assert logged[0]["book_id"] == book
    assert logged[0]["sequence"] == 1
    assert json.loads(logged[0]["detail"])["status"] == "active"


def test_a_delivery_is_recorded_with_how_much_went_out(conn, reader, sent):
    _, sub_id = reader
    conn.execute("UPDATE subscriptions SET current_sequence = 5, chunks_per_day = 3 WHERE id = ?",
                 (sub_id,))
    dispatch.process_subscription(sub_id, trigger="next", conn=conn)

    logged = [row for row in rows(conn) if row["event"] == "delivered"]
    assert len(logged) == 1
    assert logged[0]["sequence"] == 5
    detail = json.loads(logged[0]["detail"])
    assert detail["parts"] == 3
    assert detail["trigger"] == "next"


def test_pause_resume_and_preferences_are_recorded(client, conn, reader):
    user_id, sub_id = reader
    token = security.generate_dashboard_token(user_id)

    client.post("/toggle_pause", data={"token": token, "sub_id": sub_id},
                follow_redirects=False)
    client.post("/toggle_pause", data={"token": token, "sub_id": sub_id},
                follow_redirects=False)
    client.post("/update_preferences", data={
        "token": token, "sub_id": sub_id, "timezone": "US/Pacific",
        "delivery_hour": 7, "chunks_per_day": 4}, follow_redirects=False)

    assert [row["event"] for row in rows(conn)] == ["paused", "resumed", "prefs_changed"]
    detail = json.loads(rows(conn)[2]["detail"])
    assert detail["chunks_per_day"] == 4 and detail["delivery_hour"] == 7


def test_unsubscribing_is_recorded_once(client, conn, reader):
    _, sub_id = reader
    token = security.generate_unsub_token(sub_id)

    client.post("/unsubscribe", data={"token": token})
    client.post("/unsubscribe", data={"token": token})   # already gone

    assert [row["event"] for row in rows(conn)] == ["unsubscribed"]


# --- the promise ---

def test_no_event_anywhere_carries_identity(client, conn, book, sent):
    """The policy says this log is attached to the subscription, not the
    reader. Walk a whole session and check that stays true."""
    email = "traceable@example.com"
    client.post("/signup", data={"email": email, "book_id": book})

    user = UserManager(conn).get_user_by_email(email)
    sub_id = conn.execute("SELECT id FROM subscriptions WHERE user_id = ?",
                          (user["id"],)).fetchone()["id"]
    token = security.generate_dashboard_token(user["id"])

    dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    client.post("/toggle_pause", data={"token": token, "sub_id": sub_id},
                follow_redirects=False)
    client.post("/unsubscribe", data={"token": security.generate_unsub_token(sub_id)})

    blob = " ".join(
        " ".join(str(value) for value in dict(row).values()) for row in rows(conn))

    assert len(rows(conn)) >= 4
    assert email not in blob
    assert "traceable" not in blob
    assert user["email_lookup"] not in blob      # the stable pseudonym
    assert token not in blob
    assert "@" not in blob


def test_deleting_a_subscription_severs_its_events_rather_than_erasing_them(conn, reader, sent):
    """Section 6 promises deletion; the metrics should survive it anonymously."""
    _, sub_id = reader
    dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    before = len(rows(conn))
    assert before > 0

    conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))

    after = rows(conn)
    assert len(after) == before                       # still there
    assert all(row["subscription_id"] is None for row in after)   # but unlinkable
    assert all(row["event"] and row["occurred_at"] for row in after)
