"""The public numbers, and the gate in front of the ranking."""
import stats
from user_manager import UserManager

BOOK_ID = "pgtest"


def subscribe(conn, email, book_id=BOOK_ID):
    user_id, _ = UserManager(conn).get_or_create_user(email)
    conn.execute(
        """INSERT INTO subscriptions
               (user_id, book_id, status, current_sequence, chunks_per_day,
                delivery_hour, last_sent, created_at)
           VALUES (?, ?, 'active', 1, 2, 6, NULL, '2026-01-01T00:00:00+00:00')""",
        (user_id, book_id),
    )
    return user_id


# --- the shelf ---

def test_library_facts_count_the_shelf(conn, book):
    facts = stats.library_facts(conn)

    assert facts["books"] == 1
    assert facts["authors"] == 1
    assert facts["words"] == 3200          # 10 chunks of 320
    assert facts["parts"] == 10
    assert facts["longest"]["title"] == "Test Book"


def test_library_years_turn_parts_into_time(conn, book):
    """Ten parts at two a morning is five days, which rounds to 0.0 years -
    the sum is what matters, and it must not blow up on a small library."""
    assert stats.library_facts(conn)["years"] == 0.0


def test_an_empty_library_still_answers(conn):
    facts = stats.library_facts(conn)

    assert facts["books"] == 0
    assert facts["words"] == 0
    assert facts["longest"] is None


# --- what has gone out ---

def test_served_words_count_the_parts_actually_sent(conn, book):
    """Each delivery carries its own span: the event says where it started and
    how many parts it took, and the words come from those chunks."""
    subscribe(conn, "reader@example.com")
    sub_id = conn.execute(
        "SELECT id FROM subscriptions ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO events (occurred_at, event, subscription_id, book_id,
                               sequence, detail)
           VALUES ('2026-01-02T06:00:00+00:00', 'delivered', ?, ?, 1,
                   '{"parts": 2, "trigger": "cron"}')""",
        (sub_id, book),
    )
    conn.execute(
        """INSERT INTO events (occurred_at, event, subscription_id, book_id,
                               sequence, detail)
           VALUES ('2026-01-03T06:00:00+00:00', 'delivered', ?, ?, 3,
                   '{"parts": 1, "trigger": "next"}')""",
        (sub_id, book),
    )

    assert stats.served_facts(conn)["words"] == 320 * 3


def test_nothing_served_is_zero_not_an_error(conn, book):
    assert stats.served_facts(conn) == {"books": 0, "words": 0}


def test_books_served_counts_books_handed_out_not_titles(conn, book):
    subscribe(conn, "one@example.com")
    subscribe(conn, "two@example.com")

    # One title, two readers: two books served.
    assert stats.served_facts(conn)["books"] == 2


# --- the gate ---

def test_the_ranking_is_absent_below_the_threshold(conn, book):
    subscribe(conn, "lonely@example.com")

    assert stats.popular_books(conn, books_served=1) is None


def test_the_ranking_is_absent_exactly_at_the_threshold(conn, book):
    """At the threshold, not past it: the gate opens on the hundred-and-first."""
    assert stats.popular_books(conn, stats.POPULARITY_THRESHOLD) is None
    assert stats.popular_books(conn, stats.POPULARITY_THRESHOLD + 1) is not None


def test_the_ranking_orders_by_readers(conn, book):
    conn.execute(
        """INSERT INTO books (id, title, author, total_chunks, total_words, created_at)
           VALUES ('pgquiet', 'A Quiet Book', 'Someone', 5, 500,
                   '2026-01-01T00:00:00+00:00')"""
    )
    subscribe(conn, "a@example.com")
    subscribe(conn, "b@example.com")
    subscribe(conn, "c@example.com", "pgquiet")

    ranking = stats.popular_books(conn, stats.POPULARITY_THRESHOLD + 1)

    assert [(r["title"], r["readers"]) for r in ranking] == [
        ("Test Book", 2), ("A Quiet Book", 1)]


# --- the page ---

def test_the_stats_page_shows_the_library(client, book):
    page = client.get("/stats").text

    assert "words waiting to be read" in page
    assert "3,200" in page


def test_the_stats_page_hides_the_ranking_while_it_would_name_people(client, conn, book):
    subscribe(conn, "lonely@example.com")

    page = client.get("/stats").text

    assert "Most popular books" not in page
    # Not the title - /library shows every title to anyone. What must not
    # appear is a title next to a reader count.
    assert "rank-row" not in page


def test_the_stats_page_shows_the_ranking_once_the_gate_opens(
        client, conn, book, monkeypatch):
    monkeypatch.setattr(stats, "POPULARITY_THRESHOLD", 0)
    subscribe(conn, "a@example.com")

    page = client.get("/stats").text

    assert "Most popular books" in page
    assert "Test Book" in page
