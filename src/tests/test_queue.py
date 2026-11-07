"""Rearranging the queue, and taking books off it."""
import db
import dispatch
import reading_queue
import security
from user_manager import UserManager


def add_queued(conn, user_id, book_id, title, position=None, sequence=1,
               created="2026-01-02T00:00:00+00:00"):
    conn.execute(
        """INSERT OR IGNORE INTO books
               (id, title, author, total_chunks, total_words, created_at)
           VALUES (?, ?, 'Someone', 60, 18000, '2026-01-01T00:00:00+00:00')""",
        (book_id, title),
    )
    return conn.execute(
        """INSERT INTO subscriptions
               (user_id, book_id, status, current_sequence, chunks_per_day,
                delivery_hour, queue_position, created_at)
           VALUES (?, ?, 'queued', ?, 2, 6, ?, ?)""",
        (user_id, book_id, sequence, position, created),
    ).lastrowid


def order(conn, user_id):
    return [row["id"] for row in reading_queue.queued(conn, user_id)]


def status_of(conn, sub_id):
    return conn.execute("SELECT status FROM subscriptions WHERE id = ?",
                        (sub_id,)).fetchone()["status"]


# --- rearranging ---

def test_moving_a_book_up_swaps_it_with_the_one_above(conn, reader):
    user_id, _ = reader
    first = add_queued(conn, user_id, "pgone", "First", position=1)
    second = add_queued(conn, user_id, "pgtwo", "Second", position=2)

    assert reading_queue.move(conn, second, user_id, "up")

    assert order(conn, user_id) == [second, first]


def test_moving_a_book_down_swaps_it_with_the_one_below(conn, reader):
    user_id, _ = reader
    first = add_queued(conn, user_id, "pgone", "First", position=1)
    second = add_queued(conn, user_id, "pgtwo", "Second", position=2)

    assert reading_queue.move(conn, first, user_id, "down")

    assert order(conn, user_id) == [second, first]


def test_the_book_at_the_top_cannot_go_up(conn, reader):
    user_id, _ = reader
    first = add_queued(conn, user_id, "pgone", "First", position=1)
    add_queued(conn, user_id, "pgtwo", "Second", position=2)

    assert reading_queue.move(conn, first, user_id, "up") is False


def test_the_book_at_the_bottom_cannot_go_down(conn, reader):
    user_id, _ = reader
    add_queued(conn, user_id, "pgone", "First", position=1)
    last = add_queued(conn, user_id, "pgtwo", "Second", position=2)

    assert reading_queue.move(conn, last, user_id, "down") is False


def test_a_queue_that_predates_positions_still_reorders(conn, reader):
    """Rows inherited from before the column existed have no position at all.
    Exchanging two NULLs would change nothing, so the order is written back
    whole rather than swapped in place."""
    user_id, _ = reader
    first = add_queued(conn, user_id, "pgone", "First",
                       position=None, created="2026-01-02T00:00:00+00:00")
    second = add_queued(conn, user_id, "pgtwo", "Second",
                        position=None, created="2026-01-03T00:00:00+00:00")

    assert reading_queue.move(conn, second, user_id, "up")

    assert order(conn, user_id) == [second, first]


def test_a_reader_cannot_reorder_someone_elses_queue(conn, reader):
    user_id, _ = reader
    mine = add_queued(conn, user_id, "pgone", "First", position=1)
    add_queued(conn, user_id, "pgtwo", "Second", position=2)
    stranger, _ = UserManager(conn).get_or_create_user("stranger@example.com")

    assert reading_queue.move(conn, mine, stranger, "down") is False
    assert order(conn, user_id)[0] == mine


def test_a_paused_book_is_not_in_the_line(conn, reader):
    """Paused means set aside, not waiting its turn, so it has nowhere to move."""
    user_id, _ = reader
    sub = add_queued(conn, user_id, "pgone", "First", position=1)
    conn.execute("UPDATE subscriptions SET status = 'paused' WHERE id = ?", (sub,))

    assert reading_queue.move(conn, sub, user_id, "up") is False
    assert reading_queue.move(conn, sub, user_id, "down") is False


def test_a_direction_we_do_not_recognise_moves_nothing(conn, reader):
    user_id, _ = reader
    first = add_queued(conn, user_id, "pgone", "First", position=1)
    add_queued(conn, user_id, "pgtwo", "Second", position=2)

    assert reading_queue.move(conn, first, user_id, "sideways") is False
    assert order(conn, user_id)[0] == first


# --- taking a book off the list ---

def test_removing_a_book_keeps_where_the_reader_got_to(conn, reader):
    user_id, _ = reader
    sub = add_queued(conn, user_id, "pgone", "First", position=1, sequence=40)

    gone = reading_queue.remove(conn, sub, user_id)

    assert gone["current_sequence"] == 40
    row = conn.execute(
        "SELECT status, current_sequence, queue_position FROM subscriptions WHERE id = ?",
        (sub,)).fetchone()
    assert row["status"] == "unsubscribed"
    assert row["current_sequence"] == 40, "the place is the point of keeping the row"
    assert row["queue_position"] is None


def test_removing_from_the_middle_closes_the_gap(conn, reader):
    user_id, _ = reader
    first = add_queued(conn, user_id, "pgone", "First", position=1)
    middle = add_queued(conn, user_id, "pgtwo", "Second", position=2)
    last = add_queued(conn, user_id, "pgthree", "Third", position=3)

    reading_queue.remove(conn, middle, user_id)

    assert order(conn, user_id) == [first, last]
    assert [row["queue_position"] for row in reading_queue.queued(conn, user_id)] == [1, 2]


def test_the_book_arriving_now_is_not_on_the_list(conn, reader):
    """Stopping the active book is what /unsubscribe is for."""
    user_id, sub_id = reader

    assert reading_queue.remove(conn, sub_id, user_id) is None
    assert status_of(conn, sub_id) == "active"


def test_a_reader_cannot_remove_someone_elses_book(conn, reader):
    user_id, _ = reader
    mine = add_queued(conn, user_id, "pgone", "First", position=1)
    stranger, _ = UserManager(conn).get_or_create_user("stranger@example.com")

    assert reading_queue.remove(conn, mine, stranger) is None
    assert status_of(conn, mine) == "queued"


def test_a_removed_book_put_back_resumes_rather_than_restarts(client, conn, reader):
    user_id, _ = reader
    sub = add_queued(conn, user_id, "pgone", "First", position=1, sequence=40)
    reading_queue.remove(conn, sub, user_id)

    client.post("/queue_book", data={
        "token": security.generate_dashboard_token(user_id), "book_id": "pgone"})

    rows = conn.execute(
        "SELECT id, status, current_sequence FROM subscriptions "
        "WHERE user_id = ? AND book_id = 'pgone'", (user_id,)).fetchall()
    assert len(rows) == 1, "a second subscription would strand the reader's place"
    assert rows[0]["status"] == "queued"
    assert rows[0]["current_sequence"] == 40


def test_a_removed_book_is_offered_again_in_the_library(client, conn, reader):
    user_id, _ = reader
    sub = add_queued(conn, user_id, "pgone", "First", position=1)
    reading_queue.remove(conn, sub, user_id)
    client.get(f"/profile?token={security.generate_dashboard_token(user_id)}")

    page = client.get("/library").text

    assert "Add to my queue" in page


# --- what arrives next ---

def test_finishing_promotes_the_book_the_reader_put_first(conn, reader, sent, monkeypatch):
    """Not the one added first: the one they moved to the top."""
    monkeypatch.setattr(dispatch.ai, "get_recommendations", lambda *a, **k: [])
    user_id, sub_id = reader
    added_first = add_queued(conn, user_id, "pgearly", "Added First",
                             position=2, created="2026-01-02T00:00:00+00:00")
    moved_up = add_queued(conn, user_id, "pgwanted", "Wanted First",
                          position=1, created="2026-01-03T00:00:00+00:00")
    conn.execute("UPDATE subscriptions SET current_sequence = 11 WHERE id = ?", (sub_id,))

    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn)

    assert ok, message
    assert status_of(conn, moved_up) == "active"
    assert status_of(conn, added_first) == "queued"


def test_promotion_keeps_a_part_read_books_place(conn, reader, sent, monkeypatch):
    """A book set aside at part forty comes back at part forty.

    It used to be reset to part one on promotion, so finishing one book threw
    away everything the reader had read of the next.
    """
    monkeypatch.setattr(dispatch.ai, "get_recommendations", lambda *a, **k: [])
    user_id, sub_id = reader
    aside = add_queued(conn, user_id, "pgaside", "Set Aside", position=1, sequence=40)
    conn.execute("UPDATE subscriptions SET current_sequence = 11 WHERE id = ?", (sub_id,))

    ok, message = dispatch.process_subscription(sub_id, trigger="next", conn=conn)

    assert ok, message
    row = conn.execute(
        "SELECT status, current_sequence, queue_position FROM subscriptions WHERE id = ?",
        (aside,)).fetchone()
    assert row["status"] == "active"
    assert row["current_sequence"] == 40
    assert row["queue_position"] is None, "the book arriving now is not in the queue"


# --- the routes ---

def test_the_move_route_reorders(client, conn, reader):
    user_id, _ = reader
    first = add_queued(conn, user_id, "pgone", "First", position=1)
    second = add_queued(conn, user_id, "pgtwo", "Second", position=2)

    client.post("/queue/move", data={
        "token": security.generate_dashboard_token(user_id),
        "sub_id": second, "direction": "up"})

    assert order(conn, user_id) == [second, first]


def test_the_remove_route_takes_the_book_off(client, conn, reader):
    user_id, _ = reader
    sub = add_queued(conn, user_id, "pgone", "First", position=1)

    client.post("/queue/remove", data={
        "token": security.generate_dashboard_token(user_id), "sub_id": sub})

    assert status_of(conn, sub) == "unsubscribed"
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE event = 'unsubscribed'").fetchone()[0] == 1


def test_the_queue_routes_refuse_a_token_we_did_not_sign(client, conn, reader):
    user_id, _ = reader
    sub = add_queued(conn, user_id, "pgone", "First", position=1)

    assert client.post("/queue/move", data={
        "token": "nonsense", "sub_id": sub, "direction": "up"}).status_code == 400
    assert client.post("/queue/remove", data={
        "token": "nonsense", "sub_id": sub}).status_code == 400
    assert status_of(conn, sub) == "queued"


def test_the_dashboard_blunts_the_arrows_at_the_ends(client, conn, reader):
    user_id, _ = reader
    add_queued(conn, user_id, "pgone", "First", position=1)
    add_queued(conn, user_id, "pgtwo", "Second", position=2)

    page = client.get(f"/profile?token={security.generate_dashboard_token(user_id)}").text

    # Two books, so exactly two arrows do nothing: the first one's up and the
    # last one's down.
    assert page.count("disabled") == 2


# --- the column itself ---

def test_adding_the_column_again_is_harmless(conn):
    """init_db runs on every connection, so the migration must be a no-op
    every time after the first."""
    db.add_missing_columns(conn)
    db.add_missing_columns(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")}
    assert "queue_position" in columns
