"""The public numbers.

Two kinds of fact live here and they are not the same kind of fact.

The library ones - how many books, how many words, how long the longest is -
describe a shelf. No reader appears in them at any size, so they are published
as they stand.

The served ones count what has gone out, and the ranking counts readers per
book. A ranking by reader count is only an aggregate while the counts are
large: with three subscribers, "most popular" is a list of what three
identifiable people are reading, and calling it a statistic does not make it
one. So the ranking stays hidden until enough books have been served for a
place in it to say something about readers in general rather than about one
of them in particular.
"""

# Books served before the popularity ranking is published at all. Books, not
# readers: a ranking is drawn from subscriptions, so that is the count that
# decides whether its rows describe a crowd or a person.
POPULARITY_THRESHOLD = 100

# The site's own default, used to turn a pile of parts into a span of time.
PARTS_PER_DAY = 2


def library_facts(conn):
    """The shelf, with nobody standing in front of it."""
    totals = conn.execute(
        """SELECT COUNT(*) AS books,
                  COUNT(DISTINCT author) AS authors,
                  COALESCE(SUM(total_words), 0) AS words,
                  COALESCE(SUM(total_chunks), 0) AS parts
           FROM books"""
    ).fetchone()

    longest = conn.execute(
        "SELECT title, author, total_words FROM books "
        "WHERE total_words > 0 ORDER BY total_words DESC LIMIT 1"
    ).fetchone()
    shortest = conn.execute(
        "SELECT title, author, total_words FROM books "
        "WHERE total_words > 0 ORDER BY total_words LIMIT 1"
    ).fetchone()

    facts = dict(totals)
    # The number that makes the others mean something: a shelf this size is
    # not a reading list, it is a sentence about time.
    facts["years"] = round(facts["parts"] / PARTS_PER_DAY / 365.25, 1)
    facts["longest"] = dict(longest) if longest else None
    facts["shortest"] = dict(shortest) if shortest else None
    return facts


def served_facts(conn):
    """What has actually left the building.

    books counts subscriptions that have had at least one part emailed, not
    subscriptions started. A queued book has been chosen, not served: nothing
    went out, nobody read anything, and counting it would let one reader with
    a long queue inflate the number - and open the ranking's gate - without a
    single email being sent.

    It counts subscriptions rather than titles because the same book served to
    forty readers is forty books served, and because a count of titles would
    cap at the size of the library and could never clear a threshold near it.

    One honest consequence: a deletion request severs an event from its
    subscription (ON DELETE SET NULL, see the schema), so a reader exercising
    it takes their books back out of this count and the number can fall. That
    is the schema keeping the promise /privacy makes, and it is the right way
    round - the alternative is a counter that remembers people who asked to be
    forgotten.

    words counts the text of the parts that were really sent, by reading each
    delivery's own span out of the event log rather than assuming every
    delivery carried the current setting.

    finished counts subscriptions read to the end, which on a site about
    finishing long books slowly is the number that means the most and moves
    the least.
    """
    books = conn.execute(
        """SELECT COUNT(DISTINCT subscription_id) FROM events
           WHERE event = 'delivered' AND subscription_id IS NOT NULL"""
    ).fetchone()[0]

    finished = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE status = 'completed'"
    ).fetchone()[0]
    words = conn.execute(
        """SELECT COALESCE(SUM(c.word_count), 0)
           FROM events e
           JOIN chunks c
             ON c.book_id = e.book_id
            AND c.sequence >= e.sequence
            AND c.sequence < e.sequence
                             + COALESCE(json_extract(e.detail, '$.parts'), 0)
           WHERE e.event = 'delivered'"""
    ).fetchone()[0]

    return {"books": books, "finished": finished, "words": words}


def popular_books(conn, books_served, limit=10):
    """The ranking, or None while it would describe individuals.

    None rather than an empty list, because the page should draw nothing at
    all: an empty "most popular" section advertises a small readership just as
    loudly as naming it would.
    """
    if books_served <= POPULARITY_THRESHOLD:
        return None

    rows = conn.execute(
        """SELECT b.id AS book_id, b.title, b.author, COUNT(*) AS readers
           FROM subscriptions s
           JOIN books b ON b.id = s.book_id
           GROUP BY s.book_id
           ORDER BY readers DESC, b.title
           LIMIT ?""",
        (limit,),
    ).fetchall()

    return [dict(row) for row in rows]


def public_stats(conn):
    served = served_facts(conn)
    return {
        "library": library_facts(conn),
        "served": served,
        "popular": popular_books(conn, served["books"]),
    }
