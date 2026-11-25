"""One-click unsubscribe (RFC 8058): the header on every part, and the endpoint."""
import json
import re

import dispatch
import security


def status_of(conn, sub_id):
    return conn.execute("SELECT status FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()[0]


def one_click_url(headers):
    match = re.fullmatch(r"<https://dailylitbits\.com(/unsubscribe/one-click\?token=[^>]+)>",
                         headers["List-Unsubscribe"])
    assert match, headers["List-Unsubscribe"]
    return match.group(1)


def test_every_part_carries_the_one_click_headers(conn, reader, sent):
    _, sub_id = reader
    ok, _ = dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    assert ok
    headers = sent[-1]["headers"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    token = one_click_url(headers).split("token=")[1]
    assert int(security.verify_unsub_token(token)) == sub_id


def test_one_click_post_unsubscribes_at_once(conn, reader, sent, client):
    _, sub_id = reader
    dispatch.process_subscription(sub_id, trigger="next", conn=conn)
    url = one_click_url(sent[-1]["headers"])
    r = client.post(url, data={"List-Unsubscribe": "One-Click"}, follow_redirects=False)
    assert r.status_code == 200
    assert status_of(conn, sub_id) == "unsubscribed"
    ev = conn.execute("SELECT detail FROM events WHERE subscription_id = ? AND event = 'unsubscribed'",
                      (sub_id,)).fetchall()
    assert len(ev) == 1 and json.loads(ev[0]["detail"]) == {"trigger": "one_click"}


def test_one_click_is_idempotent(conn, reader, client):
    _, sub_id = reader
    url = f"/unsubscribe/one-click?token={security.generate_unsub_token(sub_id)}"
    assert client.post(url, data={"List-Unsubscribe": "One-Click"}).status_code == 200
    assert client.post(url, data={"List-Unsubscribe": "One-Click"}).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM events WHERE subscription_id = ? AND event = 'unsubscribed'",
                        (sub_id,)).fetchone()[0] == 1


def test_one_click_get_changes_nothing(conn, reader, client):
    """A link scanner fetching the header URL must not unsubscribe anyone."""
    _, sub_id = reader
    r = client.get(f"/unsubscribe/one-click?token={security.generate_unsub_token(sub_id)}")
    assert r.status_code == 200 and "Unsubscribe?" in r.text
    assert status_of(conn, sub_id) == "active"


def test_one_click_rejects_bad_and_wrong_purpose_tokens(conn, reader, client):
    _, sub_id = reader
    assert client.post("/unsubscribe/one-click?token=rubbish").status_code == 400
    # A next-part token for the same subscription must not work here.
    other = security.generate_next_token(sub_id)
    assert client.post(f"/unsubscribe/one-click?token={other}").status_code == 400
    assert status_of(conn, sub_id) == "active"


def test_one_click_needs_no_session(conn, reader):
    """Providers post without cookies; a fresh client has none."""
    from fastapi.testclient import TestClient
    import main
    _, sub_id = reader
    fresh = TestClient(main.app, base_url="https://testserver")
    r = fresh.post(f"/unsubscribe/one-click?token={security.generate_unsub_token(sub_id)}",
                   data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and status_of(conn, sub_id) == "unsubscribed"


def test_headers_reach_the_smtp_message(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set_debuglevel(self, *a): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): seen.update(msg.items())
    monkeypatch.setattr(dispatch.smtplib, "SMTP", FakeSMTP)
    assert dispatch.send_via_brevo("someone@example.com", "s", "<p>b</p>",
                                   headers=dispatch.unsubscribe_headers(7))
    assert seen["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert seen["List-Unsubscribe"].startswith("<https://dailylitbits.com/unsubscribe/one-click?token=")
