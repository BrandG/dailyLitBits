"""The reader's queue: what order it is in, and how that order changes.

The order used to be whatever created_at happened to say, which was never a
decision anybody made. Worse, dispatch rewrites created_at when it promotes a
book - the column holding the order was also the column meaning "when did this
book start arriving", so finishing a book quietly reshuffled what was left.

queue_position separates the two. It is the reader's stated order and nothing
else writes it.

Module name: not queue.py. The app directory is first on sys.path, so a file
called queue.py would shadow the standard library's for every import in the
process.
"""


def queued(conn, user_id):
    """Everything waiting its turn, in the reader's own order.

    created_at and id break ties, so a queue that predates queue_position - or
    one half-filled by a failed write - still comes back in a stable order
    rather than whatever the page happens to read first.
    """
    return conn.execute(
        """SELECT id, book_id, queue_position FROM subscriptions
           WHERE user_id = ? AND status = 'queued'
           ORDER BY queue_position, created_at, id""",
        (user_id,),
    ).fetchall()


def back_position(conn, user_id):
    """Behind everything already queued. Where a newly chosen book goes."""
    highest = conn.execute(
        "SELECT MAX(queue_position) FROM subscriptions "
        "WHERE user_id = ? AND status = 'queued'",
        (user_id,),
    ).fetchone()[0]
    return (highest or 0) + 1


def front_position(conn, user_id):
    """In front of everything already queued.

    Where a book goes when it steps aside for another: the one just put down
    is the one its reader is most likely to want back first.
    """
    lowest = conn.execute(
        "SELECT MIN(queue_position) FROM subscriptions "
        "WHERE user_id = ? AND status = 'queued'",
        (user_id,),
    ).fetchone()[0]
    return (lowest or 1) - 1


def renumber(conn, ordered_ids):
    """Write a whole order back, 1..n."""
    for position, sub_id in enumerate(ordered_ids, start=1):
        conn.execute(
            "UPDATE subscriptions SET queue_position = ? WHERE id = ?",
            (position, sub_id),
        )


def reorder(conn, user_id, ordered_ids):
    """Write a whole new order for a queue. True if it was written.

    The list has to be exactly the queue the reader is looking at: the same
    rows, none missing, none repeated, nothing from anywhere else. Anything
    else is refused outright rather than partly applied.

    That strictness is the point. A list missing a row would leave that row's
    position untouched and the resulting order ambiguous; a list holding an id
    this reader does not own is either a page looking at a queue that has
    since changed, or someone trying their luck. Neither should be guessed at.
    """
    current = [row["id"] for row in queued(conn, user_id)]

    # Sorted comparison catches all four refusals at once: a missing row, a
    # repeated one, an extra one, and an id belonging to somebody else.
    if sorted(ordered_ids) != sorted(current):
        return False

    renumber(conn, ordered_ids)
    return True


def move(conn, sub_id, user_id, direction):
    """Swap a queued book with its neighbour. True if anything moved.

    The whole order is written back rather than two values exchanged: on a
    queue inherited from before this column existed, positions can be NULL or
    equal, and swapping two of those changes nothing visible.
    """
    if direction not in ("up", "down"):
        return False

    order = [row["id"] for row in queued(conn, user_id)]
    if sub_id not in order:
        return False

    here = order.index(sub_id)
    there = here - 1 if direction == "up" else here + 1
    if not 0 <= there < len(order):
        return False  # already at the end it was asked to move towards

    order[here], order[there] = order[there], order[here]
    renumber(conn, order)
    return True


def remove(conn, sub_id, user_id):
    """Take a book off the list. Returns the row that went, or None.

    The row stays, marked unsubscribed, because it holds how far the reader
    got. Putting the book back later resumes it rather than starting again.
    Nothing else counts an unsubscribed row: not the library's badges, not
    the stats, not the dispatch.

    Only a queued or paused book can go this way. The one currently arriving
    is not on the list - stopping that is what /unsubscribe is for.
    """
    sub = conn.execute(
        """SELECT id, book_id, status, current_sequence FROM subscriptions
           WHERE id = ? AND user_id = ? AND status IN ('queued', 'paused')""",
        (sub_id, user_id),
    ).fetchone()

    if not sub:
        return None

    conn.execute(
        "UPDATE subscriptions SET status = 'unsubscribed', queue_position = NULL "
        "WHERE id = ?",
        (sub_id,),
    )
    renumber(conn, [row["id"] for row in queued(conn, user_id)])
    return sub


def revive(conn, user_id, book_id, status="queued"):
    """An unsubscribed book put back on the list, or None if there isn't one.

    Without this, removing a book and adding it again would insert a second
    subscription starting at part one, stranding the reader's place in a row
    nothing reads afterwards.

    It keeps current_sequence, so a book taken off the list at part forty goes
    back on at part forty.
    """
    sub = conn.execute(
        """SELECT id, current_sequence FROM subscriptions
           WHERE user_id = ? AND book_id = ? AND status = 'unsubscribed'
           ORDER BY id DESC LIMIT 1""",
        (user_id, book_id),
    ).fetchone()

    if not sub:
        return None

    # Only a queued book holds a position; the one arriving now is not in line.
    position = back_position(conn, user_id) if status == "queued" else None
    conn.execute(
        "UPDATE subscriptions SET status = ?, queue_position = ? WHERE id = ?",
        (status, position, sub["id"]),
    )
    return sub
