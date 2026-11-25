"""Addresses this app must never email again.

A row here outranks everything else: dispatch skips the reader and retires
their subscriptions, signup sends no confirmation, and send_via_brevo itself
refuses as a last line of defence.

Rules:

1. Only the keyed hash (email_lookup) is stored, never the address, so the list
   can be kept after a reader asks for full deletion without keeping who they
   were. That is what stops a deleted complainer being re-subscribed later.
2. Rows are never removed by a sync. If Brevo drops an address from its
   blocklist (an admin unblocks it, the account changes, the provider is
   replaced), the app still remembers. Removing one is a deliberate act.
3. A sync that cannot reach Brevo, or gets an error, changes nothing and
   exits non-zero. It never treats "no answer" as "nobody is blocked".

Why this exists: Brevo already refuses to send to its own blocklist, but it
accepts the message over SMTP first and blocks it afterwards. The app saw
success, logged a delivery and advanced the reader's place in the book.
"""
import sys
from datetime import datetime, timezone

import requests

import config
import db
import events
from user_manager import UserManager

REASONS = {"complaint", "hard_bounce", "unsubscribed", "invalid", "manual", "other"}

# Brevo's blockedContacts reason codes, mapped onto ours.
BREVO_REASONS = {
    "contactFlaggedAsSpam": "complaint",
    "hardBounce": "hard_bounce",
    "unsubscribedViaEmail": "unsubscribed",
    "unsubscribedViaApi": "unsubscribed",
    "unsubscribedViaMA": "unsubscribed",
    "invalidEmail": "invalid",
    "adminBlocked": "manual",
}

PAGE_SIZE = 100


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lookup_for(conn, email):
    return UserManager(conn).email_lookup(email)


def reason_for(conn, lookup):
    """The stored reason if this lookup is suppressed, else None."""
    row = conn.execute("SELECT reason FROM suppressions WHERE email_lookup = ?",
                       (lookup,)).fetchone()
    return row["reason"] if row else None


def is_suppressed(conn, email):
    return reason_for(conn, lookup_for(conn, email)) is not None


def add(conn, lookup, reason, source, occurred_at=None):
    """Records one suppression. True if it is new. An existing row is kept
    as it was: the first reason and date are the ones that matter."""
    if reason not in REASONS:
        raise ValueError(f"unknown suppression reason {reason!r}")
    return conn.execute(
        """INSERT OR IGNORE INTO suppressions (email_lookup, reason, source, occurred_at, recorded_at)
           VALUES (?, ?, ?, ?, ?)""",
        (lookup, reason, source, occurred_at, utc_now())).rowcount == 1


def stop_deliveries(conn, lookup, reason):
    """Retires every live subscription for this address. Reading position is
    kept; only the status changes. Returns how many were stopped."""
    subs = conn.execute(
        """SELECT s.id, s.book_id FROM subscriptions s JOIN users u ON u.id = s.user_id
           WHERE u.email_lookup = ? AND s.status IN ('active', 'queued', 'paused')""",
        (lookup,)).fetchall()
    for sub in subs:
        conn.execute("UPDATE subscriptions SET status = 'unsubscribed' WHERE id = ?", (sub["id"],))
        events.record(conn, "unsubscribed", subscription_id=sub["id"], book_id=sub["book_id"],
                      trigger="suppressed", reason=reason)
    return len(subs)


def fetch_brevo_blocklist():
    """Every address on Brevo's transactional blocklist. Raises on any error,
    so a partial or failed fetch is never mistaken for the whole list."""
    if not config.BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY is not set")
    headers = {"api-key": config.BREVO_API_KEY, "accept": "application/json"}
    contacts, offset = [], 0
    while True:
        r = requests.get(f"{config.BREVO_API_URL}/smtp/blockedContacts", headers=headers,
                         params={"limit": PAGE_SIZE, "offset": offset}, timeout=30)
        r.raise_for_status()
        body = r.json()
        page = body.get("contacts") or []
        contacts += page
        offset += PAGE_SIZE
        if not page or offset >= body.get("count", 0):
            return contacts


def sync_brevo(conn, contacts=None):
    """Brings Brevo's blocklist into the app. Returns counts, no addresses."""
    if contacts is None:
        contacts = fetch_brevo_blocklist()
    counts = {"seen": 0, "new": 0, "stopped": 0}
    with db.transaction(conn):
        for contact in contacts:
            email = contact.get("email")
            if not email:
                continue
            counts["seen"] += 1
            code = (contact.get("reason") or {}).get("code")
            reason = BREVO_REASONS.get(code, "other")
            lookup = lookup_for(conn, email)
            if add(conn, lookup, reason, "brevo", contact.get("blockedAt")):
                counts["new"] += 1
            # Every time, not only for new rows: a reader can appear after
            # their address was suppressed.
            counts["stopped"] += stop_deliveries(conn, lookup, reason_for(conn, lookup))
    return counts


if __name__ == "__main__":
    conn = db.get_db()
    try:
        result = sync_brevo(conn)
    except Exception as exc:
        print(f"{utc_now()} [ERROR] Brevo suppression sync failed, nothing changed: "
              f"{type(exc).__name__}: {exc}")
        sys.exit(1)
    finally:
        conn.close()
    print(f"{utc_now()} [OK] Brevo blocklist: {result['seen']} seen, {result['new']} new, "
          f"{result['stopped']} subscription(s) stopped")
