"""Tests for the daily acquisition job and its judgment layer.

The expensive mistakes this guards against are all one-directional: taking a
book we already have, taking a dictionary, or taking something because the
adjudicator was broken. So most of these assert that nothing was acquired.
"""
import pytest

import acquire
import adjudicator
from adjudicator import Candidate, Decision, JevAdjudicator, \
    RefuseAdjudicator, ScriptedAdjudicator


# --------------------------------------------------------------- normalising

@pytest.mark.parametrize("raw,expected", [
    ("The Time Machine", "time machine"),
    ("A Tale of Two Cities", "tale of two cities"),
    ("Moby Dick; Or, The Whale", "moby dick"),
    ("Pride and Prejudice [Illustrated]", "pride and prejudice"),
    ("Alice's Adventures in Wonderland", "alice s adventures in wonderland"),
    ("Les Misérables", "miserables"),
    ("Hamlet (Cambridge Edition)", "hamlet"),
    ("  The   Iliad  ", "iliad"),
])
def test_normalise_title(raw, expected):
    assert acquire.normalise_title(raw) == expected


def test_normalise_title_handles_nothing():
    assert acquire.normalise_title("") == ""
    assert acquire.normalise_title(None) == ""


@pytest.mark.parametrize("raw,expected", [
    ("Austen, Jane", "austen"),
    ("Jane Austen", "austen"),
    ("Shakespeare, William", "shakespeare"),
    ("Doyle, Arthur Conan", "doyle"),
    ("", ""),
])
def test_surname(raw, expected):
    assert acquire.surname(raw) == expected


def test_display_author_flips_the_catalogue_order():
    assert acquire.display_author([{"name": "Austen, Jane"}]) == "Jane Austen"
    assert acquire.display_author([{"name": "Various"}]) == "Various"
    assert acquire.display_author([]) == "Unknown Author"


# ------------------------------------------------------- stage one: the screen

def rec(**kw):
    base = {
        "id": 9999,
        "title": "A Perfectly Ordinary Novel",
        "authors": [{"name": "Nobody, A."}],
        "languages": ["en"],
        "media_type": "Text",
        "copyright": False,
        "subjects": ["Fiction"],
        "bookshelves": [],
        "download_count": 1000,
        "formats": {"text/plain; charset=utf-8":
                    "https://www.gutenberg.org/ebooks/9999.txt.utf-8"},
    }
    base.update(kw)
    return base


def test_screen_passes_an_ordinary_novel():
    decision, reason = acquire.screen(rec())
    assert decision is None
    assert reason == "needs_adjudication"


@pytest.mark.parametrize("title", [
    "Webster's Unabridged Dictionary",
    "An Encyclopaedia of Gardening",
    "A Concordance to the Works of Shakespeare",
    "Index of the Project Gutenberg Works",
    "The Complete Works of William Shakespeare",
    "Moby Multiple Language Lists of Common Words",
    "Scientific American, Volume 22, No. 1",
    "Punch, or the London Charivari, Volume 1",
])
def test_screen_rejects_reference_and_serial_titles(title):
    decision, reason = acquire.screen(rec(title=title))
    assert decision == "not_serial", f"{title!r} got {decision} ({reason})"


def test_screen_does_not_reject_an_innocent_lookalike():
    # "contradiction" contains "diction" but is not a reference word, and the
    # matcher is word-based rather than substring-based for exactly this case.
    decision, _ = acquire.screen(rec(title="A Study in Contradiction"))
    assert decision is None


def test_screen_rejects_on_catalogue_subject_alone():
    decision, reason = acquire.screen(
        rec(title="Chambers's Twentieth Century Book",
            subjects=["English language -- Dictionaries"]))
    assert decision == "not_serial"
    assert reason.startswith("subject:")


@pytest.mark.parametrize("patch,expected_reason_prefix", [
    ({"media_type": "Sound"}, "media_type"),
    ({"copyright": True}, "still_in_copyright"),
    ({"languages": ["fr"]}, "language"),
    ({"formats": {"text/html": "x"}}, "no_plain_text"),
    ({"title": ""}, "empty_title"),
])
def test_screen_rejects_unsuitable(patch, expected_reason_prefix):
    decision, reason = acquire.screen(rec(**patch))
    assert decision == "unsuitable"
    assert reason.startswith(expected_reason_prefix)


def test_screen_ignores_a_zipped_text_file():
    decision, reason = acquire.screen(
        rec(formats={"text/plain": "https://example.com/pg1.txt.zip"}))
    assert (decision, reason) == ("unsuitable", "no_plain_text")


# ------------------------------------------------------- duplicates and shortlist

def held(*pairs):
    return [{"book_id": f"pg{i}", "title": t, "author": a,
             "norm": acquire.normalise_title(t),
             "tokens": acquire.title_tokens(acquire.normalise_title(t)),
             "surname": acquire.surname(a)}
            for i, (t, a) in enumerate(pairs, start=1)]


def test_exact_duplicate_sees_through_articles_and_brackets():
    library = held(("The Time Machine", "H. G. Wells"))
    assert acquire.exact_duplicate(
        acquire.normalise_title("Time Machine [Illustrated]"), library)
    assert acquire.exact_duplicate(
        acquire.normalise_title("The War of the Worlds"), library) is None


def test_shortlist_puts_the_same_author_first():
    library = held(("Hamlet", "William Shakespeare"),
                   ("Emma", "Jane Austen"),
                   ("Moby Dick", "Herman Melville"))
    picked = acquire.shortlist_for(
        acquire.normalise_title("Romeo and Juliet"), "shakespeare", library)
    assert picked[0] == ("Hamlet", "William Shakespeare")


def test_shortlist_is_bounded():
    library = held(*[(f"Book {n}", "Jane Austen") for n in range(40)])
    picked = acquire.shortlist_for("book 3", "austen", library, n=5)
    assert len(picked) == 5


def test_shortlist_never_comes_back_empty_when_the_library_is_not():
    library = held(("Hamlet", "William Shakespeare"))
    picked = acquire.shortlist_for("zorblatt treatise", "pfeng", library)
    assert picked == [("Hamlet", "William Shakespeare")]


# ------------------------------------------------------------ the job end to end

@pytest.fixture
def catalogue(monkeypatch):
    """Replaces the live catalogue with a fixed list. Returns the list so a
    test can rewrite it."""
    records = []

    def fake(max_pages=None, pause=0, stats=None):
        # Mirrors the real walker's contract: ranks come out one-based and
        # `stats` is filled in rather than returned, so run()'s reporting is
        # exercised rather than stubbed past.
        if stats is not None:
            stats["pages_read"] = 1
            stats["pages_failed"] = 0
            stats["ended"] = "end_of_catalogue"
        for n, r in enumerate(records, start=1):
            yield n, r

    monkeypatch.setattr(acquire, "catalogue", fake)
    return records


@pytest.fixture
def no_download(monkeypatch):
    """Stubs the download+chunk step. Returns the list of ids it was asked for."""
    asked = []

    def fake(conn, record, title, author, *, with_recaps, max_bytes):
        asked.append(record["id"])
        book_id = f"pg{record['id']}"
        conn.execute(
            """INSERT INTO books (id, title, author, total_chunks, total_words,
                                  created_at)
               VALUES (?, ?, ?, 42, 13000, '2026-01-01T00:00:00+00:00')""",
            (book_id, title, author))
        return True, (book_id, 42)

    monkeypatch.setattr(acquire, "ingest_candidate", fake)
    return asked


def test_takes_one_book_and_logs_it(conn, catalogue, no_download):
    catalogue.append(rec(id=100, title="A Fine Novel"))
    taken = acquire.run(conn=conn, adj=ScriptedAdjudicator({100: "NEW"}))

    assert [t[0] for t in taken] == [100]
    assert no_download == [100]
    row = conn.execute(
        "SELECT * FROM acquisitions WHERE gutenberg_id = 100").fetchone()
    assert row["decision"] == "acquired"
    assert row["stage"] == "jev"
    ev = conn.execute("SELECT * FROM events WHERE event = 'acquired'").fetchone()
    assert ev["book_id"] == "pg100"
    assert ev["subscription_id"] is None


def test_stops_at_the_requested_count(conn, catalogue, no_download):
    for n in range(5):
        catalogue.append(rec(id=200 + n, title=f"Fine Novel the {n}th"))
    taken = acquire.run(conn=conn, count=1, adj=ScriptedAdjudicator({}))
    assert len(taken) == 1
    assert len(no_download) == 1


def test_a_duplicate_verdict_is_recorded_and_not_taken(conn, catalogue, no_download):
    catalogue.append(rec(id=300, title="Hamlet, Prince of Denmark"))
    taken = acquire.run(conn=conn, adj=ScriptedAdjudicator({300: "DUPLICATE"}))

    assert taken == []
    assert no_download == []
    row = conn.execute(
        "SELECT decision, stage FROM acquisitions WHERE gutenberg_id = 300"
    ).fetchone()
    assert (row["decision"], row["stage"]) == ("duplicate", "jev")


def test_a_broken_adjudicator_acquires_nothing(conn, catalogue, no_download):
    catalogue.append(rec(id=400, title="A Fine Novel"))
    taken = acquire.run(conn=conn, adj=ScriptedAdjudicator({400: None}))

    assert taken == []
    assert no_download == []
    # Nothing written down either: the candidate was never judged, so it must
    # stay available for a later run.
    assert conn.execute("SELECT COUNT(*) c FROM acquisitions").fetchone()["c"] == 0


def test_no_key_configured_acquires_nothing(conn, catalogue, no_download):
    catalogue.append(rec(id=500, title="A Fine Novel"))
    taken = acquire.run(conn=conn, adj=RefuseAdjudicator())
    assert (taken, no_download) == ([], [])


def test_deterministic_rejections_cost_no_adjudicator_calls(conn, catalogue,
                                                            no_download):
    catalogue.extend([
        rec(id=601, title="Webster's Complete Dictionary"),
        rec(id=602, title="Scientific American, Volume 3, No. 7"),
        rec(id=603, title="A Novel That Is Fine", copyright=True),
    ])
    adj = ScriptedAdjudicator({})
    taken = acquire.run(conn=conn, adj=adj)

    assert taken == []
    assert adj.calls == 0
    decisions = dict(conn.execute(
        "SELECT gutenberg_id, decision FROM acquisitions").fetchall())
    assert decisions == {601: "not_serial", 602: "not_serial", 603: "unsuitable"}


def test_a_judged_candidate_is_never_paid_for_twice(conn, catalogue, no_download):
    catalogue.append(rec(id=700, title="A Book We Already Hold"))
    first = ScriptedAdjudicator({700: "DUPLICATE"})
    acquire.run(conn=conn, adj=first)
    assert first.calls == 1

    second = ScriptedAdjudicator({700: "DUPLICATE"})
    acquire.run(conn=conn, adj=second)
    assert second.calls == 0, "the second run re-judged a settled candidate"


def test_books_already_in_the_library_are_skipped_for_free(conn, catalogue,
                                                           no_download):
    conn.execute(
        """INSERT INTO books (id, title, author, total_chunks, total_words,
                              created_at)
           VALUES ('pg800', 'Held Already', 'Someone', 10, 3000,
                   '2026-01-01T00:00:00+00:00')""")
    catalogue.append(rec(id=800, title="Held Already"))
    adj = ScriptedAdjudicator({})
    assert acquire.run(conn=conn, adj=adj) == []
    assert adj.calls == 0


def test_an_exact_title_match_needs_no_adjudicator(conn, catalogue, no_download):
    conn.execute(
        """INSERT INTO books (id, title, author, total_chunks, total_words,
                              created_at)
           VALUES ('pg99', 'The Time Machine', 'H. G. Wells', 10, 3000,
                   '2026-01-01T00:00:00+00:00')""")
    catalogue.append(rec(id=900, title="Time Machine [Illustrated]"))
    adj = ScriptedAdjudicator({})

    assert acquire.run(conn=conn, adj=adj) == []
    assert adj.calls == 0
    row = conn.execute(
        "SELECT decision, reason FROM acquisitions WHERE gutenberg_id = 900"
    ).fetchone()
    assert row["decision"] == "duplicate"
    assert "pg99" in row["reason"]


def test_dry_run_writes_nothing(conn, catalogue, no_download):
    catalogue.append(rec(id=1000, title="A Fine Novel"))
    taken = acquire.run(conn=conn, dry_run=True, adj=ScriptedAdjudicator({}))

    assert len(taken) == 1
    assert no_download == []
    assert conn.execute("SELECT COUNT(*) c FROM acquisitions").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"] == 0


def test_a_failed_download_is_recorded_as_retryable(conn, catalogue, monkeypatch):
    catalogue.append(rec(id=1100, title="A Fine Novel"))
    monkeypatch.setattr(acquire, "ingest_candidate",
                        lambda *a, **k: (False, "too_large:99999999"))

    assert acquire.run(conn=conn, adj=ScriptedAdjudicator({})) == []
    row = conn.execute(
        "SELECT decision FROM acquisitions WHERE gutenberg_id = 1100").fetchone()
    assert row["decision"] == "failed"
    # 'failed' is excluded from the skip set, so tomorrow tries again.
    assert 1100 not in acquire.already_decided(conn)


def test_a_second_book_in_one_run_cannot_duplicate_the_first(conn, catalogue,
                                                             no_download):
    catalogue.extend([rec(id=1200, title="The Time Machine"),
                      rec(id=1201, title="Time Machine [Illustrated]")])
    adj = ScriptedAdjudicator({})
    taken = acquire.run(conn=conn, count=2, adj=adj)

    assert [t[0] for t in taken] == [1200]
    row = conn.execute(
        "SELECT decision FROM acquisitions WHERE gutenberg_id = 1201").fetchone()
    assert row["decision"] == "duplicate"


# -------------------------------------------------------------- the adjudicator

class FakeAnswer:
    def __init__(self, choice, prob, confidence=None):
        self.choice = choice
        self.probabilities = {choice: prob}
        self.confidence = confidence if confidence is not None else prob


class FakeScore:
    def __init__(self, score):
        self.score = score
        self.confidence = 0.9


class FakeResponse:
    def __init__(self, choice="NEW", prob=0.99, conviction=2.0, tokens=700,
                 model="jev-1.13.0"):
        self.choices = {"verdict": FakeAnswer(choice, prob)}
        self.scores = {"conviction": FakeScore(conviction)}
        self.usage = type("U", (), {"input_tokens": tokens})()
        self.model = model


class FakeClient:
    def __init__(self, response=None, raises=None):
        self.response = response or FakeResponse()
        self.raises = raises
        self.calls = 0

    def system_one(self, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response


def jev(client, **kw):
    return JevAdjudicator(api_key="k", model="jev-1.13.0", client=client, **kw)


CAND = Candidate(gutenberg_id=1, title="A Novel", author="A Writer")


def test_jev_accepts_a_confident_new():
    d = jev(FakeClient(FakeResponse("NEW", 0.99, 2.0))).judge(CAND, [])
    assert d.ok and d.accept and d.verdict == "NEW"
    assert d.cost_usd > 0


def test_jev_never_accepts_a_duplicate():
    d = jev(FakeClient(FakeResponse("DUPLICATE", 1.0, 2.4))).judge(CAND, [])
    assert d.ok and not d.accept and d.verdict == "DUPLICATE"


def test_jev_gates_a_new_it_is_unsure_of():
    d = jev(FakeClient(FakeResponse("NEW", 0.80, 2.0))).judge(CAND, [])
    assert d.ok and not d.accept
    assert d.reason.startswith("low_probability")


def test_jev_gates_a_new_with_weak_conviction():
    d = jev(FakeClient(FakeResponse("NEW", 0.99, 1.0))).judge(CAND, [])
    assert d.ok and not d.accept
    assert d.reason.startswith("low_conviction")


def test_jev_refuses_an_off_menu_answer():
    d = jev(FakeClient(FakeResponse("MAYBE", 1.0, 2.0))).judge(CAND, [])
    assert not d.ok and not d.accept and d.reason == "off_menu"


def test_jev_fails_closed_on_a_transport_error():
    d = jev(FakeClient(raises=TimeoutError("slow"))).judge(CAND, [])
    assert not d.ok and not d.accept
    assert d.reason == "TimeoutError"


def test_jev_fails_closed_on_a_malformed_answer():
    bad = FakeResponse()
    bad.scores = {}
    d = jev(FakeClient(bad)).judge(CAND, [])
    assert not d.ok and not d.accept
    assert d.reason.startswith("malformed:")


def test_jev_stops_at_the_spend_cap():
    client = FakeClient(FakeResponse(tokens=10_000_000))
    adj = jev(client, daily_usd_cap=0.0001)
    assert adj.judge(CAND, []).ok          # the first call is what spends it
    second = adj.judge(CAND, [])
    assert not second.ok and second.reason == "daily_spend_cap"
    assert client.calls == 1


def test_jev_insists_on_a_pinned_model():
    with pytest.raises(ValueError):
        JevAdjudicator(api_key="k", model="", client=FakeClient())
    with pytest.raises(ValueError):
        JevAdjudicator(api_key="", model="jev-1.13.0", client=FakeClient())


def test_state_sends_the_shortlist_not_the_library():
    state = CAND.state([("Hamlet", "William Shakespeare")])
    assert state["already_held"] == [{"title": "Hamlet",
                                      "author": "William Shakespeare"}]
    assert state["candidate_title"] == "A Novel"


def test_from_config_refuses_without_a_key():
    cfg = type("C", (), {"JEV_API_KEY": None})()
    assert isinstance(adjudicator.from_config(cfg), RefuseAdjudicator)
    assert not adjudicator.from_config(cfg).judge(CAND, []).accept


def test_refuse_adjudicator_is_the_default_not_accept_everything():
    assert not RefuseAdjudicator().judge(CAND, []).accept


MUST_PASS = [
    "The Number of the Beast",
    "Atlas Shrugged",
    "A Study in Scarlet",
    "The Grapes of Wrath",
    "Twenty Thousand Leagues Under the Sea",
    "The Picture of Dorian Gray",
    "Alice's Adventures in Wonderland",
    "A Journal of the Plague Year",
    "The Call of the Wild",
    "Notes from the Underground",
    "The Man Who Was Thursday",
    "Three Men in a Boat",
    "The Count of Monte Cristo",
    "Anne of Green Gables",
    "The Jungle Book",
    "Heart of Darkness",
    "The Mysterious Island",
    "A Christmas Carol",
]


@pytest.mark.parametrize("title", MUST_PASS)
def test_the_filter_does_not_reject_real_books(title):
    """A false reject is silent and costs a book, so the keyword list gets a
    standing guard against growing teeth."""
    decision, reason = acquire.screen(rec(title=title))
    assert decision is None, f"{title!r} was rejected as {decision} ({reason})"


# ---------------------------------------------------- catalogue title plumbing

@pytest.mark.parametrize("raw,expected", [
    # Both of these came off the live popularity ranking. '$b' is a MARC
    # subfield delimiter, and it would have been stored as part of the title.
    ("The silent Baltic : $b or, Detained near Kiel",
     "The silent Baltic: or, Detained near Kiel"),
    ("The Nuttall encyclopaedia : $b being a concise dictionary",
     "The Nuttall encyclopaedia: being a concise dictionary"),
    ("A Normal Title", "A Normal Title"),
    ("Trailing punctuation :", "Trailing punctuation"),
    ("Collapsed    spacing", "Collapsed spacing"),
    (None, ""),
])
def test_clean_catalogue_title(raw, expected):
    assert acquire.clean_catalogue_title(raw) == expected


def test_ligatures_normalise_so_the_keyword_filter_still_fires():
    # NFKD leaves 'ae' alone, so without the explicit mapping the punctuation
    # strip turned 'Encyclopaedia' into 'encyclopdia' and the filter missed it.
    assert acquire.normalise_title("Encyclop\u00e6dia of Gardening") == \
        "encyclopaedia of gardening"
    decision, reason = acquire.screen(rec(title="Encyclop\u00e6dia of Gardening"))
    assert decision == "not_serial"


def test_the_stored_title_is_the_cleaned_one(conn, catalogue, no_download):
    catalogue.append(rec(id=1300, title="A Book : $b With Plumbing"))
    acquire.run(conn=conn, adj=ScriptedAdjudicator({}))
    row = conn.execute("SELECT title FROM books WHERE id = 'pg1300'").fetchone()
    assert row["title"] == "A Book: With Plumbing"


# ------------------------------------------------- persisting to one a day

def test_a_dead_catalogue_page_does_not_end_the_walk(monkeypatch, conn,
                                                     no_download):
    """The bug this guards is the whole point of the change.

    The old walker followed the catalogue's own `next` links, so the link to
    page N+1 only existed inside page N: one page that timed out took the
    entire rest of the ranking with it, and the day ended with no book for a
    reason that had nothing to do with any book. Page 2 here is dead, and the
    only takeable book is on page 3.
    """
    pages = {
        1: {"results": [rec(id=101, title="A Dictionary of Things"),
                        rec(id=102, title="An Encyclopaedia of Other Things")],
            "next": "whatever"},
        2: TimeoutError("read operation timed out"),
        3: {"results": [rec(id=303, title="The Book On Page Three")],
            "next": None},
    }

    def fake_fetch(url, attempts=3, backoff=0, timeout=None):
        page_no = int(url.rsplit("page=", 1)[1])
        got = pages.get(page_no)
        assert got is not None, f"walked past the fixture: page {page_no}"
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(acquire, "fetch_json", fake_fetch)
    monkeypatch.setattr(acquire.time, "sleep", lambda *_: None)

    taken = acquire.run(conn=conn, max_pages=3, adj=ScriptedAdjudicator({}))

    assert [t[0] for t in taken] == [303], "the book after the dead page"


def test_a_dead_catalogue_is_not_mistaken_for_an_empty_one(monkeypatch):
    """Consecutive dead pages mean the catalogue is down, not that there is
    nothing left to read. The walk gives up early and names which it was."""
    def always_dead(url, attempts=3, backoff=0, timeout=None):
        raise TimeoutError("read operation timed out")

    monkeypatch.setattr(acquire, "fetch_json", always_dead)
    monkeypatch.setattr(acquire.time, "sleep", lambda *_: None)

    stats = {}
    assert list(acquire.catalogue(max_pages=50, stats=stats)) == []
    assert stats["ended"] == "catalogue_unreachable"
    assert stats["pages_failed"] == acquire.PAGE_FAILURE_LIMIT, \
        "gave up after the limit instead of walking all 50"


def test_a_skipped_page_does_not_shift_the_ranks_after_it(monkeypatch):
    """Rank comes from the page number, so a skipped page leaves everything
    below it correctly ranked rather than a page too high."""
    def fetch(url, attempts=3, backoff=0, timeout=None):
        if int(url.rsplit("page=", 1)[1]) == 1:
            raise TimeoutError("dead")
        return {"results": [rec(id=1), rec(id=2)], "next": None}

    monkeypatch.setattr(acquire, "fetch_json", fetch)
    monkeypatch.setattr(acquire.time, "sleep", lambda *_: None)

    ranks = [rank for rank, _ in acquire.catalogue(max_pages=3)]
    assert ranks == [acquire.PAGE_SIZE + 1, acquire.PAGE_SIZE + 2]


def test_a_reachable_end_of_catalogue_is_reported_as_such(monkeypatch):
    def fetch(url, attempts=3, backoff=0, timeout=None):
        return {"results": [rec(id=7)], "next": None}

    monkeypatch.setattr(acquire, "fetch_json", fetch)
    stats = {}
    assert len(list(acquire.catalogue(max_pages=9, stats=stats))) == 1
    assert stats["ended"] == "end_of_catalogue"
    assert stats["pages_read"] == 1, "stopped at the end, not at the bound"


def test_it_keeps_judging_until_it_finds_one(conn, catalogue, no_download):
    """Rejections are not a reason to stop. Four adjudicated rejections in a
    row, then a book: the run has to come back with the book."""
    for n in range(5):
        catalogue.append(rec(id=400 + n, title=f"Plausible Novel the {n}th"))
    adj = ScriptedAdjudicator({400: "DUPLICATE", 401: "NOT_SERIAL",
                               402: "DUPLICATE", 403: "NOT_SERIAL",
                               404: "NEW"})
    taken = acquire.run(conn=conn, count=1, adj=adj)

    assert [t[0] for t in taken] == [404], "the fifth candidate"
    assert adj.calls == 5


def test_a_repeatedly_broken_adjudicator_stops_the_walk(conn, catalogue,
                                                        no_download):
    """Persisting past a rejection is right; persisting past an outage just
    spends the timeout budget on the same non-answer."""
    for n in range(40):
        catalogue.append(rec(id=500 + n, title=f"Plausible Novel the {n}th"))
    taken = acquire.run(conn=conn, count=1,
                        adj=RefuseAdjudicator("timeout"))

    assert taken == []
    judged = conn.execute(
        "SELECT COUNT(*) c FROM acquisitions").fetchone()["c"]
    assert judged == 0, "an outage is not a verdict about any book"


def test_the_search_budget_does_not_count_time_spent_ingesting(
        monkeypatch, conn, catalogue, no_download):
    """A long book's recaps run to twenty minutes. That is work already won,
    and it must not be charged against the hunt for the next one."""
    monkeypatch.setattr(acquire, "SEARCH_BUDGET_S", 5)
    for n in range(3):
        catalogue.append(rec(id=600 + n, title=f"Plausible Novel the {n}th"))

    real = acquire.ingest_candidate
    clock = {"t": 0.0}
    monkeypatch.setattr(acquire.time, "monotonic", lambda: clock["t"])

    def slow(*a, **kw):
        clock["t"] += 600.0        # ten minutes of recaps
        return real(*a, **kw)

    monkeypatch.setattr(acquire, "ingest_candidate", slow)
    taken = acquire.run(conn=conn, count=2, adj=ScriptedAdjudicator({}))

    assert len(taken) == 2, "the second book was still searched for"


def test_one_exploding_candidate_does_not_cost_the_day_s_book(
        monkeypatch, conn, catalogue, no_download):
    """Unexpected is not the same as fatal. The old loop let any exception out
    of the process, which meant a traceback out of cron and no book."""
    catalogue.append(rec(id=700, title="The Candidate That Explodes"))
    catalogue.append(rec(id=701, title="A Perfectly Fine Novel"))

    real_screen = acquire.screen

    def exploding(record):
        if record["id"] == 700:
            raise RuntimeError("something nobody foresaw")
        return real_screen(record)

    monkeypatch.setattr(acquire, "screen", exploding)
    taken = acquire.run(conn=conn, count=1, adj=ScriptedAdjudicator({}))

    assert [t[0] for t in taken] == [701]


def test_a_storm_of_errors_stops_the_walk(monkeypatch, conn, catalogue,
                                          no_download):
    """Tolerating one error is right; grinding through two thousand
    candidates' worth of the same error is not."""
    for n in range(40):
        catalogue.append(rec(id=800 + n, title=f"Plausible Novel the {n}th"))

    def always_explodes(record):
        raise RuntimeError("the database is on fire")

    monkeypatch.setattr(acquire, "screen", always_explodes)
    taken = acquire.run(conn=conn, count=1, adj=ScriptedAdjudicator({}))

    assert taken == []
    assert no_download == [], "nothing was ingested on the back of an error"
