"""The suppression list: Brevo sync, and every path that would send mail."""
import json

import pytest

import dispatch
import suppression
from conftest import BOOK_ID
from user_manager import UserManager

READER = "reader@example.com"


def blocked(email, code="contactFlaggedAsSpam", at="2026-11-20T10:00:00+01:00"):
    return {"email": email, "reason": {"code": code, "message": "x"},
            "blockedAt": at, "senderEmail": "pages@dailylitbits.com"}


def lookup(conn, email):
    return UserManager(conn).email_lookup(email)


def status_of(conn, sub_id):
    return conn.execute("SELECT status FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()[0]


def test_sync_adds_maps_reasons_and_stops_subscriptions(conn, reader):
    _, sub_id = reader
    counts = suppression.sync_brevo(conn, [
        blocked(READER, "contactFlaggedAsSpam"),
        blocked("gone@example.com", "hardBounce"),
        blocked("left@example.com", "unsubscribedViaEmail"),
        blocked("odd@example.com", "somethingNew"),
    ])
    assert counts == {"seen": 4, "new": 4, "stopped": 1}
    assert suppression.reason_for(conn, lookup(conn, READER)) == "complaint"
    assert suppression.reason_for(conn, lookup(conn, "gone@example.com")) == "hard_bounce"
    assert suppression.reason_for(conn, lookup(conn, "left@example.com")) == "unsubscribed"
    assert suppression.reason_for(conn, lookup(conn, "odd@example.com")) == "other"
    assert status_of(conn, sub_id) == "unsubscribed"
    # Reading position is kept.
    assert conn.execute("SELECT current_sequence FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()[0] == 1
    ev = conn.execute("SELECT event, detail FROM events WHERE subscription_id = ?",
                      (sub_id,)).fetchall()
    assert [e["event"] for e in ev] == ["unsubscribed"]
    assert json.loads(ev[0]["detail"]) == {"trigger": "suppressed", "reason": "complaint"}


def test_no_address_is_stored(conn):
    suppression.sync_brevo(conn, [blocked(READER)])
    dump = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM suppressions"))
    assert "@" not in dump and "reader" not in dump


def test_sync_is_idempotent_and_never_forgets(conn, reader):
    suppression.sync_brevo(conn, [blocked(READER)])
    again = suppression.sync_brevo(conn, [blocked(READER)])
    assert again == {"seen": 1, "new": 0, "stopped": 0}
    # Brevo forgets the address; the app does not.
    suppression.sync_brevo(conn, [])
    assert suppression.is_suppressed(conn, READER)


def test_first_reason_wins(conn):
    suppression.sync_brevo(conn, [blocked(READER, "hardBounce")])
    suppression.sync_brevo(conn, [blocked(READER, "contactFlaggedAsSpam")])
    assert suppression.reason_for(conn, lookup(conn, READER)) == "hard_bounce"


def test_address_matching_is_normalised(conn):
    suppression.sync_brevo(conn, [blocked("  Reader@Example.COM ")])
    assert suppression.is_suppressed(conn, READER)


def test_failed_fetch_changes_nothing(conn, monkeypatch):
    import config

    class Boom:
        status_code = 401
        def raise_for_status(self):
            raise RuntimeError("401 unrecognised IP")
    monkeypatch.setattr(config, "BREVO_API_KEY", "xkeysib-test")
    monkeypatch.setattr(suppression.requests, "get", lambda *a, **k: Boom())
    with pytest.raises(RuntimeError):
        suppression.sync_brevo(conn)
    assert conn.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0] == 0


def test_missing_key_refuses(conn, monkeypatch):
    import config
    monkeypatch.setattr(config, "BREVO_API_KEY", None)
    with pytest.raises(RuntimeError):
        suppression.sync_brevo(conn)


def test_fetch_pages_through_the_list(monkeypatch):
    import config
    monkeypatch.setattr(config, "BREVO_API_KEY", "xkeysib-test")
    everyone = [blocked(f"r{i}@example.com") for i in range(230)]
    calls = []

    class Resp:
        def __init__(self, params):
            self.params = params
        def raise_for_status(self):
            pass
        def json(self):
            o, n = self.params["offset"], self.params["limit"]
            return {"count": len(everyone), "contacts": everyone[o:o + n]}

    def fake_get(url, headers, params, timeout):
        calls.append(params["offset"])
        assert headers["api-key"] == "xkeysib-test"
        return Resp(params)
    monkeypatch.setattr(suppression.requests, "get", fake_get)
    assert len(suppression.fetch_brevo_blocklist()) == 230
    assert calls == [0, 100, 200]


def test_dispatch_skips_and_retires_a_suppressed_reader(conn, reader, sent):
    _, sub_id = reader
    suppression.add(conn, lookup(conn, READER), "complaint", "test")
    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert not ok and "Suppressed" in message
    assert sent == []
    assert status_of(conn, sub_id) == "unsubscribed"


def test_dispatch_still_sends_to_everyone_else(conn, reader, sent):
    _, sub_id = reader
    suppression.add(conn, lookup(conn, "someone.else@example.com"), "complaint", "test")
    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok and len(sent) == 1


def test_send_via_brevo_refuses_a_suppressed_address(conn, monkeypatch):
    suppression.add(conn, lookup(conn, READER), "hard_bounce", "test")

    def no_smtp(*a, **k):
        raise AssertionError("SMTP must not be opened for a suppressed address")
    monkeypatch.setattr(dispatch.smtplib, "SMTP", no_smtp)
    assert dispatch.send_via_brevo(READER, "s", "<p>b</p>") is False


def test_signup_for_a_suppressed_address_sends_nothing(conn, book, client, sent):
    suppression.add(conn, lookup(conn, READER), "complaint", "test")
    normal = client.post("/signup", data={"email": "fresh@example.com", "book_id": BOOK_ID})
    sent.clear()
    r = client.post("/signup", data={"email": READER, "book_id": BOOK_ID})
    assert r.status_code == 200
    assert sent == []
    assert conn.execute("SELECT COUNT(*) FROM pending_signups WHERE email_lookup = ?",
                        (lookup(conn, READER),)).fetchone()[0] == 0
    # Same answer as a normal signup, so the form reveals nothing.
    assert "Almost there" in r.text and "Almost there" in normal.text


def test_confirm_refused_if_suppressed_after_signup(conn, book, client, sent):
    from conftest import confirm_token_from
    client.post("/signup", data={"email": READER, "book_id": BOOK_ID})
    token = confirm_token_from(sent[-1]["body"])
    suppression.add(conn, lookup(conn, READER), "complaint", "test")
    sent.clear()
    r = client.post("/confirm", data={"token": token})
    assert "can't send to this address" in r.text
    assert sent == []
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
