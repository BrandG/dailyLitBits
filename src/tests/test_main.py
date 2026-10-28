"""The HTTP surface: signing up, the dashboard, the email links, admin."""
import re

import main
import security
from user_manager import UserManager


def dashboard_token_from(email_body):
    match = re.search(r"/profile\?token=([^\"]+)", email_body)
    assert match, "no dashboard link in the email"
    return match.group(1)


# --- public pages ---

def test_homepage_lists_the_library(client, book):
    response = client.get("/")
    assert response.status_code == 200
    assert "Test Book" in response.text


def test_library_page_renders(client, book):
    response = client.get("/library")
    assert response.status_code == 200
    assert "Test Book" in response.text


# --- signing up ---

def test_signup_creates_a_reader_and_mails_a_link(client, conn, book, sent):
    response = client.post("/signup", data={
        "email": "new@example.com", "book_id": book, "timezone": "US/Eastern"})

    assert response.status_code == 200
    assert "starts tomorrow" in response.text

    manager = UserManager(conn)
    user = manager.get_user_by_email("new@example.com")
    assert user is not None
    assert user["timezone"] == "US/Eastern"
    assert user["role"] == "reader"

    sub = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user["id"],)).fetchone()
    assert sub["status"] == "active"
    assert sub["current_sequence"] == 1

    assert len(sent) == 1
    assert security.verify_dashboard_token(dashboard_token_from(sent[0]["body"])) == str(user["id"])


def test_a_second_book_is_queued_not_started(client, conn, book, sent):
    conn.execute(
        """INSERT INTO books (id, title, author, total_chunks, total_words, created_at)
           VALUES ('pgtwo', 'Second Book', 'Author', 4, 1200, '2026-01-01T00:00:00+00:00')""")

    client.post("/signup", data={"email": "new@example.com", "book_id": book})
    response = client.post("/signup", data={"email": "new@example.com", "book_id": "pgtwo"})

    assert "queue" in response.text.lower()
    statuses = dict(conn.execute(
        """SELECT book_id, status FROM subscriptions s
           JOIN users u ON u.id = s.user_id WHERE u.email_lookup = ?""",
        (UserManager(conn).email_lookup("new@example.com"),)).fetchall())
    assert statuses[book] == "active"
    assert statuses["pgtwo"] == "queued"


def test_signing_up_for_the_same_book_twice_is_refused_politely(client, conn, book, sent):
    client.post("/signup", data={"email": "new@example.com", "book_id": book})
    response = client.post("/signup", data={"email": "new@example.com", "book_id": book})

    assert response.status_code == 200
    assert "already reading" in response.text.lower()
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1


def test_signup_for_an_unknown_book_is_refused(client, conn, book):
    response = client.post("/signup", data={"email": "new@example.com", "book_id": "nope"})
    assert "not in the library" in response.text
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


# --- getting back in ---

def test_login_mails_the_link_for_a_known_address(client, conn, book, sent):
    user_id, _ = UserManager(conn).get_or_create_user("reader@example.com")
    response = client.post("/login", data={"email": "reader@example.com"})

    assert response.status_code == 200
    assert len(sent) == 1
    assert security.verify_dashboard_token(dashboard_token_from(sent[0]["body"])) == str(user_id)


def test_login_does_not_reveal_whether_an_address_is_known(client, conn, book, sent):
    known_user, _ = UserManager(conn).get_or_create_user("reader@example.com")
    known = client.post("/login", data={"email": "reader@example.com"})
    unknown = client.post("/login", data={"email": "stranger@example.com"})

    assert known.status_code == unknown.status_code == 200
    # Same words either way; only the mailing differs.
    assert "on its way" in known.text and "on its way" in unknown.text
    assert len(sent) == 1


# --- dashboard ---

def test_dashboard_shows_progress(client, conn, reader):
    user_id, _ = reader
    response = client.get(f"/profile?token={security.generate_dashboard_token(user_id)}")

    assert response.status_code == 200
    assert "Test Book" in response.text
    assert "Part 1 of 10" in response.text


def test_dashboard_rejects_a_bad_or_wrong_purpose_token(client, conn, reader):
    user_id, sub_id = reader
    assert client.get("/profile?token=rubbish").status_code == 400
    # An unsubscribe link must not open someone's dashboard.
    assert client.get(
        f"/profile?token={security.generate_unsub_token(sub_id)}").status_code == 400


def test_changing_delivery_size_keeps_the_readers_place(client, conn, reader):
    user_id, sub_id = reader
    conn.execute("UPDATE subscriptions SET current_sequence = 6 WHERE id = ?", (sub_id,))

    response = client.post("/update_preferences", data={
        "token": security.generate_dashboard_token(user_id),
        "sub_id": sub_id, "timezone": "US/Pacific",
        "delivery_hour": 7, "chunks_per_day": 4,
    }, follow_redirects=False)

    assert response.status_code == 303
    row = conn.execute(
        "SELECT current_sequence, chunks_per_day, delivery_hour FROM subscriptions WHERE id = ?",
        (sub_id,)).fetchone()
    assert row["chunks_per_day"] == 4
    assert row["delivery_hour"] == 7
    assert row["current_sequence"] == 6


def test_you_cannot_change_someone_elses_subscription(client, conn, reader):
    """The token says who you are; the subscription has to belong to you."""
    _, victim_sub = reader
    attacker_id, _ = UserManager(conn).get_or_create_user("attacker@example.com")

    response = client.post("/update_preferences", data={
        "token": security.generate_dashboard_token(attacker_id),
        "sub_id": victim_sub, "timezone": "UTC",
        "delivery_hour": 23, "chunks_per_day": 12,
    }, follow_redirects=False)

    assert response.status_code == 400
    row = conn.execute("SELECT delivery_hour, chunks_per_day FROM subscriptions WHERE id = ?",
                       (victim_sub,)).fetchone()
    assert row["delivery_hour"] == 6
    assert row["chunks_per_day"] == 2


def test_pause_and_resume(client, conn, reader):
    user_id, sub_id = reader
    token = security.generate_dashboard_token(user_id)

    client.post("/toggle_pause", data={"token": token, "sub_id": sub_id}, follow_redirects=False)
    assert conn.execute("SELECT status FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()["status"] == "paused"

    client.post("/toggle_pause", data={"token": token, "sub_id": sub_id}, follow_redirects=False)
    assert conn.execute("SELECT status FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()["status"] == "active"


def test_switch_book_sets_the_new_book_reading_and_queues_the_old(client, conn, reader):
    user_id, old_sub = reader
    conn.execute(
        """INSERT INTO books (id, title, author, total_chunks, total_words, created_at)
           VALUES ('pgtwo', 'Second Book', 'Author', 4, 1200, '2026-01-01T00:00:00+00:00')""")

    response = client.get(
        f"/switch_book?token={security.generate_dashboard_token(user_id)}&new_book_id=pgtwo")

    assert response.status_code == 200
    assert "Second Book" in response.text
    assert conn.execute("SELECT status FROM subscriptions WHERE id = ?",
                        (old_sub,)).fetchone()["status"] == "queued"
    assert conn.execute("SELECT status FROM subscriptions WHERE book_id = 'pgtwo'"
                        ).fetchone()["status"] == "active"


# --- email links ---

def test_next_link_sends_the_next_part(client, conn, reader, sent):
    _, sub_id = reader
    response = client.get(f"/next?token={security.generate_next_token(sub_id)}")

    assert response.status_code == 200
    assert "On its way" in response.text
    assert len(sent) == 1


def test_unsubscribe_confirms_then_unsubscribes(client, conn, reader):
    """This POST used to 500: the token carried an email, the handler wanted an id."""
    _, sub_id = reader
    token = security.generate_unsub_token(sub_id)

    confirm = client.get(f"/unsubscribe?token={token}")
    assert confirm.status_code == 200
    assert "Are you sure" in confirm.text

    done = client.post("/unsubscribe", data={"token": token})
    assert done.status_code == 200
    assert "have been unsubscribed" in done.text
    assert conn.execute("SELECT status FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()["status"] == "unsubscribed"


def test_unsubscribe_rejects_a_bad_token(client, conn, reader):
    assert client.post("/unsubscribe", data={"token": "rubbish"}).status_code == 400


# --- admin ---

def test_admin_needs_a_token(client, conn, reader):
    assert client.get("/admin").status_code == 400


def test_admin_rejects_a_dashboard_token(client, conn, reader):
    user_id, _ = reader
    UserManager(conn).set_role(user_id, "admin")
    response = client.get(f"/admin?token={security.generate_dashboard_token(user_id)}")
    assert response.status_code == 400


def test_admin_rejects_a_reader_holding_a_valid_admin_token(client, conn, reader):
    user_id, _ = reader
    response = client.get(f"/admin?token={security.generate_admin_token(user_id)}")
    assert response.status_code == 403


def test_admin_shows_subscribers_to_an_admin(client, conn, reader):
    user_id, _ = reader
    UserManager(conn).set_role(user_id, "admin")

    response = client.get(f"/admin?token={security.generate_admin_token(user_id)}")
    assert response.status_code == 200
    assert "reader@example.com" in response.text
    assert "Test Book" in response.text


def test_expired_admin_token_is_refused(client, conn, reader, monkeypatch):
    user_id, _ = reader
    UserManager(conn).set_role(user_id, "admin")
    monkeypatch.setattr(security, "ADMIN_TOKEN_MAX_AGE", -1)

    response = client.get(f"/admin?token={security.generate_admin_token(user_id)}")
    assert response.status_code == 400


# --- suggestions ---

def test_suggestion_is_stored(client, conn):
    response = client.post("/suggest", data={
        "title": "Moby Dick", "author": "Melville", "link": "", "comments": "please"})

    assert response.status_code == 200
    row = conn.execute("SELECT * FROM suggestions").fetchone()
    assert row["title"] == "Moby Dick"
    assert row["status"] == "pending"


# --- PAUSED SUBSCRIPTIONS ---

def _add_paused_book(conn, user_id, book_id="pgpaused", title="The Paused Book", at=4):
    conn.execute(
        """INSERT INTO books (id, title, author, total_chunks, total_words, created_at)
           VALUES (?, ?, 'Someone', 10, 3000, '2026-01-01T00:00:00+00:00')""",
        (book_id, title))
    conn.executemany(
        "INSERT INTO chunks (book_id, sequence, content, word_count) VALUES (?, ?, ?, ?)",
        [(book_id, n, f"paused text {n}", 300) for n in range(1, 11)])
    cur = conn.execute(
        """INSERT INTO subscriptions (user_id, book_id, status, current_sequence,
               chunks_per_day, delivery_hour, created_at)
           VALUES (?, ?, 'paused', ?, 2, 6, '2026-01-01T00:00:00+00:00')""",
        (user_id, book_id, at))
    return cur.lastrowid


def test_a_paused_book_is_visible_when_another_is_active(client, conn, reader):
    """A paused book sends no email, so the dashboard is the only way back to
    it. Listing only 'queued' left it stranded with its position intact."""
    user_id, _ = reader
    _add_paused_book(conn, user_id)

    page = client.get(f"/profile?token={security.generate_dashboard_token(user_id)}").text

    assert "Test Book" in page            # the active one still shown
    assert "The Paused Book" in page      # and the paused one is reachable
    assert "Resume" in page
    assert "part 4" in page               # with the place it was left at


def test_resuming_a_paused_book_keeps_its_place_and_queues_the_active_one(client, conn, reader):
    user_id, active_sub = reader
    paused_sub = _add_paused_book(conn, user_id)

    response = client.get(
        f"/switch_book?token={security.generate_dashboard_token(user_id)}"
        f"&new_book_id=pgpaused")
    assert response.status_code == 200

    resumed = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (paused_sub,)).fetchone()
    previous = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (active_sub,)).fetchone()

    assert resumed["status"] == "active"
    assert resumed["current_sequence"] == 4      # resumed, not restarted
    assert previous["status"] == "queued"        # one active subscription at a time
    assert previous["current_sequence"] == 1     # and it kept its own place


def test_the_current_book_is_not_also_listed_below(client, conn, reader):
    user_id, _ = reader
    page = client.get(f"/profile?token={security.generate_dashboard_token(user_id)}").text
    assert page.count("Test Book") == 1


def test_progress_label_is_not_inside_the_coloured_fill(client, conn, reader):
    """Early in a book the fill is too narrow to hold its own label - at part 5
    of 165 it rendered as 'Par'. The label belongs under the bar."""
    user_id, _ = reader
    page = client.get(f"/profile?token={security.generate_dashboard_token(user_id)}").text

    fill = re.search(r'<div class="progress-bar[^>]*>(.*?)</div>', page, re.S)
    assert fill is not None
    assert fill.group(1).strip() == ""
    assert "Part 1 of 10" in page


# --- new arrivals ---

def test_home_shows_the_newest_books_first(client, conn, book):
    """The strip is ordered by when a book entered the library, not by title."""
    conn.executemany(
        """INSERT INTO books (id, title, author, total_chunks, total_words, created_at)
           VALUES (?, ?, 'An Author', 1, 100, ?)""",
        [("pgold", "An Older Book", "2026-01-02T00:00:00+00:00"),
         ("pgnew", "The Newest Book", "2026-05-05T00:00:00+00:00")],
    )

    page = client.get("/").text

    assert "New arrivals" in page
    # Scoped to the strip: the signup dropdown below it lists every book by
    # title, so searching the whole page would compare the wrong two spots.
    strip = page.split('class="arrivals-grid"', 1)[1]
    assert strip.index("The Newest Book") < strip.index("An Older Book")


def test_home_still_renders_when_a_date_cannot_be_read(client, conn, book):
    """A date we cannot parse costs that book its label, not the whole page."""
    conn.execute("UPDATE books SET created_at = 'whenever' WHERE id = ?", (book,))

    response = client.get("/")

    assert response.status_code == 200
    assert "Test Book" in response.text


def test_cover_url_prefers_our_copy_then_gutenberg_then_nothing():
    """Nothing is a real answer: the template draws a title plate instead, so
    a book with no artwork does not reach for a third-party placeholder."""
    assert main.cover_url_for(
        {"book_id": "pg11", "cover_path": "/static/covers/11.jpg"}
    ) == "/static/covers/11.jpg"

    assert main.cover_url_for(
        {"book_id": "pg11", "cover_path": None}
    ).endswith("/pg11.cover.medium.jpg")

    assert main.cover_url_for({"book_id": "pgtest", "cover_path": None}) is None
