"""The signup form: no book is chosen for anyone, the daily email is spelled
out, and the honeypot drops bots quietly."""
from conftest import BOOK_ID


def pending_count(conn):
    return conn.execute("SELECT COUNT(*) FROM pending_signups").fetchone()[0]


def test_form_has_no_preselected_book(client, book):
    page = client.get("/").text
    assert '<option value="" selected disabled>Pick one' in page
    assert 'name="book_id" required' in page
    # The only "selected" option is the placeholder.
    assert page.count(" selected") == 1


def test_form_says_it_is_a_daily_email(client, book):
    assert "one email every morning" in client.get("/").text


def test_missing_book_is_refused_politely(client, conn, book, sent):
    r = client.post("/signup", data={"email": "a@example.com"})
    assert r.status_code == 200 and "Please choose a book" in r.text
    r = client.post("/signup", data={"email": "a@example.com", "book_id": ""})
    assert "Please choose a book" in r.text
    assert sent == [] and pending_count(conn) == 0


def test_unknown_book_is_refused(client, conn, book, sent):
    r = client.post("/signup", data={"email": "a@example.com", "book_id": "pg-nope"})
    assert "not in the library" in r.text
    assert sent == [] and pending_count(conn) == 0


def test_honeypot_drops_the_signup_but_answers_as_usual(client, conn, book, sent):
    bot = client.post("/signup", data={"email": "victim@example.com", "book_id": BOOK_ID,
                                       "website": "http://spam.example"})
    person = client.post("/signup", data={"email": "person@example.com", "book_id": BOOK_ID,
                                          "website": ""})
    assert "Almost there" in bot.text and "Almost there" in person.text
    assert [m["to"] for m in sent] == ["person@example.com"]
    assert pending_count(conn) == 1


def test_honeypot_field_is_hidden_from_people(client, book):
    page = client.get("/").text
    assert 'name="website"' in page and 'tabindex="-1"' in page and 'aria-hidden="true"' in page
