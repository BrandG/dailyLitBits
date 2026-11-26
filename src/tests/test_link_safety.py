"""The /profile and email-link audit: headers, no tokens in our own links,
and limits on the dashboard-link form."""
import config
import security
from conftest import BOOK_ID


def test_private_pages_are_never_cached_and_carry_security_headers(client, conn, reader):
    user_id, _ = reader
    token = security.generate_dashboard_token(user_id)
    r = client.get(f"/profile?token={token}", follow_redirects=True)
    assert r.status_code == 200
    for resp in (*r.history, r):
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Referrer-Policy"] == "same-origin"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_public_page_without_a_session_may_be_cached(client, book):
    r = client.get("/")
    assert "Cache-Control" not in r.headers
    assert r.headers["Referrer-Policy"] == "same-origin"


def test_token_links_are_no_store(client, conn, reader):
    _, sub_id = reader
    r = client.get(f"/unsubscribe?token={security.generate_unsub_token(sub_id)}")
    assert r.headers["Cache-Control"] == "no-store"


def test_dashboard_and_library_links_carry_no_token(client, conn, reader, book):
    user_id, _ = reader
    # A second book, paused, so both pages draw a switch link for it.
    conn.execute("""INSERT INTO books (id, title, author, blurb, total_chunks, total_words, created_at)
                    VALUES ('pgtwo', 'Second Book', 'Someone', 'b', 5, 1600, '2026-01-01T00:00:00+00:00')""")
    conn.execute("""INSERT INTO subscriptions (user_id, book_id, status, current_sequence,
                    chunks_per_day, delivery_hour, created_at)
                    VALUES (?, 'pgtwo', 'paused', 3, 2, 6, '2026-01-02T00:00:00+00:00')""",
                 (user_id,))
    token = security.generate_dashboard_token(user_id)
    dash = client.get(f"/profile?token={token}", follow_redirects=True).text
    lib = client.get("/library").text
    for page in (dash, lib):
        assert "/switch_book?new_book_id=" in page
        assert "/switch_book?token=" not in page


def test_switch_book_page_works_from_the_session(client, conn, reader, book):
    user_id, _ = reader
    client.get(f"/profile?token={security.generate_dashboard_token(user_id)}")
    r = client.get(f"/switch_book?new_book_id={BOOK_ID}")
    assert r.status_code == 200 and "Switch books?" in r.text
    # The POST still gets the token, in the form body.
    assert 'name="token"' in r.text


def test_switch_book_page_still_takes_a_mailed_token(client, conn, reader, book):
    user_id, _ = reader
    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app, base_url="https://testserver")
    token = security.generate_dashboard_token(user_id)
    assert "Switch books?" in fresh.get(f"/switch_book?token={token}&new_book_id={BOOK_ID}").text


def test_switch_book_page_refuses_a_stranger(book):
    from fastapi.testclient import TestClient
    import main
    fresh = TestClient(main.app, base_url="https://testserver")
    r = fresh.get(f"/switch_book?new_book_id={BOOK_ID}")
    assert "Switch books?" not in r.text


def test_dashboard_link_form_is_limited_per_address(client, conn, reader, sent, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_LINKS_PER_ADDRESS_PER_HOUR", 3)
    answers = [client.post("/login", data={"email": "reader@example.com"}).text for _ in range(6)]
    assert len(sent) == 3
    # Same answer every time.
    assert all("If that address has a subscription" in a for a in answers)


def test_dashboard_link_form_is_limited_per_ip(client, conn, reader, sent, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_LINKS_PER_IP_PER_HOUR", 2)
    for n in range(4):
        client.post("/login", data={"email": f"nobody{n}@example.com"})
    client.post("/login", data={"email": "reader@example.com"})
    assert sent == []


def test_unknown_address_gets_the_same_answer_and_no_mail(client, conn, sent):
    r = client.post("/login", data={"email": "stranger@example.com"})
    assert "If that address has a subscription" in r.text and sent == []


def test_no_get_route_changes_anything(client, conn, reader, book):
    """Every GET a mail scanner could follow, with a valid token: nothing changes."""
    user_id, sub_id = reader
    before = [tuple(r) for r in conn.execute("SELECT * FROM subscriptions ORDER BY id")]
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    for url in (f"/next?token={security.generate_next_token(sub_id)}",
                f"/unsubscribe?token={security.generate_unsub_token(sub_id)}",
                f"/unsubscribe/one-click?token={security.generate_unsub_token(sub_id)}",
                f"/switch_book?token={security.generate_dashboard_token(user_id)}&new_book_id={BOOK_ID}",
                f"/profile?token={security.generate_dashboard_token(user_id)}",
                "/library", "/", "/stats", "/login", "/privacy", "/intro", "/suggest"):
        assert client.get(url).status_code < 500, url
    assert [tuple(r) for r in conn.execute("SELECT * FROM subscriptions ORDER BY id")] == before
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
