"""Fill in missing recaps across the whole library.

`ingest.py --recaps-only <book>` does one book; this walks every book that
still has chunks without a recap. One Gemini call per chunk, sequential within
a book, which for a full library is tens of thousands of calls.

**It reports by default and generates nothing without --run.** That is
deliberate: this script is still on the nightly crontab from the Mongo era,
where it was a harmless backfill worker. Defaulting to a report means that
cron entry logs what is outstanding instead of quietly spending the budget.
"""
import argparse

import db
from ingest import generate_recaps_for_book
from logger import log


def pending_counts(conn):
    return conn.execute(
        """SELECT b.id, b.title, COUNT(c.sequence) AS missing
           FROM books b JOIN chunks c ON c.book_id = b.id
           WHERE c.recap IS NULL
           GROUP BY b.id ORDER BY b.title"""
    ).fetchall()


def backfill(conn, limit=None, dry_run=False):
    rows = pending_counts(conn)
    if not rows:
        log("Every chunk already has a recap.")
        return

    total_missing = sum(row["missing"] for row in rows)
    log(f"{total_missing} chunks across {len(rows)} books still need a recap.")

    if dry_run:
        for row in rows:
            log(f"   {row['id']:10} {row['title'][:45]:47} {row['missing']:5} calls")
        log(f"--- Report only: {total_missing} Gemini calls would be made. "
            f"Pass --run to generate. ---")
        return

    budget = limit
    for row in rows:
        if budget is not None and budget <= 0:
            log("Reached the --limit for this run; stopping.")
            break
        log(f"--- {row['title']} ({row['missing']} missing) ---")
        calls, _ = generate_recaps_for_book(conn, row["id"], limit=budget)
        if budget is not None:
            budget -= calls

    log("--- Backfill run complete. ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill recaps for the whole library")
    parser.add_argument("--limit", type=int, help="Stop after this many Gemini calls in total")
    parser.add_argument("--run", action="store_true",
                        help="Actually generate. Without this it only reports what is missing.")
    args = parser.parse_args()

    conn = db.get_db()
    try:
        backfill(conn, limit=args.limit, dry_run=not args.run)
    finally:
        conn.close()
