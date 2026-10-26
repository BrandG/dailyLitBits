"""A log of what happened, kept for metrics and for nothing else.

Three rules keep this inside the promise made on /privacy, and all three are
enforced here rather than left to whoever adds the next call site:

1. Identity never goes in. No address, no email_lookup, no token. Especially
   not email_lookup: it is a stable pseudonym across the whole system, so
   logging it once would make every row correlatable forever.
2. subscription_id is nullable on purpose, and the schema uses
   ON DELETE SET NULL. The policy promises full deletion on request; severing
   the link leaves an unlinkable aggregate row rather than destroying the
   churn data the metrics exist to measure.
3. `detail` is an allowlist, not free text. Free text is how an email address
   eventually ends up in a log.

One honest caveat: while a subscription still exists this is *pseudonymous*,
not anonymous - subscription_id joins to a user who has an encrypted address.
That is fine for running the service. It is not a claim of anonymity.
"""
import json
from datetime import datetime, timezone

# Every event this app is allowed to record.
EVENTS = {
    "subscribed",     # a reader took on a book
    "delivered",      # parts went out
    "paused",         # vacation mode on
    "resumed",        # vacation mode off, or a paused book picked back up
    "queued",         # set aside because another book became current
    "switched",       # the current book changed
    "completed",      # reached the end
    "unsubscribed",   # stopped for good
    "prefs_changed",  # delivery settings edited
}

# Everything `detail` may carry. Reading preferences and book ids only.
ALLOWED_DETAIL = {
    "chunks_per_day",
    "delivery_hour",
    "timezone",
    "status",
    "from_book",
    "to_book",
    "parts",
    "trigger",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(conn, event, subscription_id=None, book_id=None, sequence=None, **detail):
    """Writes one event. Raises rather than logging something it should not."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}; add it to events.EVENTS deliberately")

    unexpected = set(detail) - ALLOWED_DETAIL
    if unexpected:
        raise ValueError(
            f"detail keys not allowed in the event log: {sorted(unexpected)}. "
            "Add to ALLOWED_DETAIL only after checking it carries no identity."
        )

    # Cheap structural guard against the obvious mistake.
    for key, value in detail.items():
        if isinstance(value, str) and "@" in value:
            raise ValueError(f"detail[{key!r}] looks like an address; the event log takes none")

    conn.execute(
        """INSERT INTO events (occurred_at, event, subscription_id, book_id, sequence, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (utc_now(), event, subscription_id, book_id, sequence,
         json.dumps(detail, sort_keys=True) if detail else None),
    )
