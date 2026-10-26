"""Sends the daily deliveries.

A delivery is subscriptions.chunks_per_day consecutive chunks, concatenated
into one email, after which current_sequence advances by however many were
sent. Nothing here calls Gemini: the recap comes from chunks.recap, written
once at ingest. The old version made a live API call per email and ignored
the cached value.

Run by cron every hour; each subscription decides whether it is its own
delivery hour in the reader's timezone.
"""
import argparse
import random
from datetime import datetime, timedelta, timezone

import pytz

import ai
import config
import db
import security
from user_manager import UserManager

# --- CONFIGURATION ---
TARGET_HOUR = 6
NEXT_COOLDOWN_MINUTES = 5


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value):
    """SQLite holds timestamps as ISO-8601 UTC text. Returns an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else pytz.utc.localize(parsed)


def format_email_html(title, first_seq, last_seq, total_chunks, content,
                      unsub_token, next_token, dashboard_token, recap=None):
    html_content = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
    base_url = "https://dailylitbits.com"
    unsub_link = f"{base_url}/unsubscribe?token={unsub_token}"
    binge_link = f"{base_url}/next?token={next_token}"
    dashboard_link = f"{base_url}/profile?token={dashboard_token}"

    # One chunk reads "Part 7"; several read "Parts 7-9".
    part_label = (f"Part {first_seq}" if first_seq == last_seq
                  else f"Parts {first_seq}-{last_seq}")

    if total_chunks < 1: total_chunks = 1
    percent = int((last_seq / total_chunks) * 100)
    if percent < 1: percent = 1
    
    # Calculate widths for the table cells
    green_width = percent
    gray_width = 100 - percent

    recap_html = ""
    if recap and first_seq > 1:
        recap_html = f"""
        <div style="background-color: #f8f9fa; border-left: 4px solid #6c757d; padding: 15px; margin-bottom: 25px; color: #555; font-style: italic; font-size: 14px;">
            <strong style="color: #333; font-style: normal; display: block; margin-bottom: 5px;">Previously:</strong>
            {recap}
        </div>
        """

    binge_button_html = f"""
    <div style="text-align: center; margin-top: 30px;">
        <a href="{binge_link}" style="background-color: #2c3e50; color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-size: 14px; font-weight: bold;">
            Send the next part now
        </a>
        <p style="font-size: 11px; color: #999; margin-top: 5px;">(Or wait for tomorrow's delivery)</p>
    </div>
    """

    template = f"""
    <html>
    <body style="font-family: Georgia, 'Times New Roman', serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        
        <div style="border-bottom: 2px solid #eee; padding-bottom: 15px; margin-bottom: 20px;">
            <h2 style="color: #2c3e50; margin: 0 0 10px 0;">{title}</h2>
            
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-top: 10px;">
                <tr>
                    <td width="85%" style="vertical-align: middle;">
                        <table width="100%" cellpadding="0" cellspacing="0" height="8" style="height: 8px; background-color: #eeeeee; border-radius: 4px; overflow: hidden;">
                            <tr>
                                <td width="{green_width}%" height="8" style="background-color: #28a745; height: 8px; line-height: 0; font-size: 0;">&nbsp;</td>
                                <td width="{gray_width}%" height="8" style="background-color: #eeeeee; height: 8px; line-height: 0; font-size: 0;">&nbsp;</td>
                            </tr>
                        </table>
                    </td>
                    <td width="15%" style="text-align: right; font-family: sans-serif; font-size: 12px; color: #999; white-space: nowrap; padding-left: 10px;">
                        {part_label} of {total_chunks}
                    </td>
                </tr>
            </table>
        </div>

        {recap_html}
        
        <div style="font-size: 18px;">
            <p>{html_content}</p>
        </div>
        
        {binge_button_html}
        
        <hr style="border: 0; border-top: 1px solid #eee; margin-top: 30px;">
        
        <p style="font-size: 12px; color: #999; text-align: center; font-family: sans-serif;">
            <a href="{dashboard_link}" style="color: #2c3e50; font-weight: bold; text-decoration: none;">Manage Subscription</a> 
            &nbsp;|&nbsp; 
            <a href="{unsub_link}" style="color: #999;">Unsubscribe</a>
        </p>
    </body>
    </html>
    """
    return template

# --- UPGRADED VICTORY TEMPLATE (Table-Based for Email Compatibility) ---
def format_victory_email(book_title, days_taken, word_count, suggestions, dashboard_token, additional_message=None):
    base_url = "https://dailylitbits.com"

    # Grammar fix: "1 Day" vs "2 Days"
    day_label = "Day" if days_taken == 1 else "Days"

    # Create HTML for the 3 suggested books
    suggestions_html = ""
    for book in suggestions:
        link = f"{base_url}/switch_book?token={dashboard_token}&new_book_id={book['id']}"
        author = book.get('author', 'Unknown Author')

        suggestions_html += f"""
        <div style="background: #fff; border: 1px solid #ddd; padding: 15px; margin-bottom: 15px; border-radius: 5px; text-align: left;">
            <strong style="font-size: 16px; color: #2c3e50;">{book['title']}</strong>
            <div style="font-size: 12px; color: #777; margin-bottom: 8px;">by {author}</div>
            <div>
                <a href="{link}" style="color: #28a745; text-decoration: none; font-weight: bold; font-size: 14px;">
                    Start Reading &#8594;
                </a>
            </div>
        </div>
        """

    # Render additional message if provided
    message_html = ""
    if additional_message:
        message_html = f"<p style=\"margin-top: 20px; font-size: 16px;\">{additional_message}</p>"

    template = f"""
    <html>
    <body style="font-family: sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; text-align: center;">
        <h1 style="color: #28a745; margin-bottom: 10px;">Congratulations!</h1>
        <h2 style="color: #555; font-weight: normal; margin-top: 0;">You have finished<br><strong>{book_title}</strong></h2>

        {message_html}

        <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f8f9fa; border: 1px solid #ddd; border-radius: 10px; margin: 30px 0; border-collapse: separate;">
            <tr>
                <td width="50%" style="padding: 20px; text-align: center; border-right: 1px solid #ddd; vertical-align: middle;">
                    <p style="font-size: 12px; margin: 0; color: #999; text-transform: uppercase; letter-spacing: 1px;">Time Taken</p>
                    <p style="font-size: 28px; font-weight: bold; color: #2c3e50; margin: 5px 0 0 0;">{days_taken} {day_label}</p>
                </td>
                <td width="50%" style="padding: 20px; text-align: center; vertical-align: middle;">
                    <p style="font-size: 12px; margin: 0; color: #999; text-transform: uppercase; letter-spacing: 1px;">Words Read</p>
                    <p style="font-size: 28px; font-weight: bold; color: #2c3e50; margin: 5px 0 0 0;">{word_count:,}</p>
                </td>
            </tr>
        </table>

        <h3 style="color: #2c3e50; margin-top: 40px; border-bottom: 2px solid #eee; padding-bottom: 10px;">Ready for your next adventure?</h3>
        <p style="color: #666; font-size: 14px; margin-bottom: 20px;">Click a book below to start receiving it tomorrow.</p>

        <div style="background-color: #f4f4f4; padding: 20px; border-radius: 8px;">
            {suggestions_html}
        </div>

        <p style="margin-top: 30px; font-size: 12px; color: #999;">
            <a href="{base_url}" style="color: #999;">Browse Full Library</a>
        </p>
    </body>
    </html>
    """
    return template

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_via_brevo(to_email, subject, html_body):
    """Sends an email via Brevo SMTP."""
    msg = MIMEMultipart()
    msg['From'] = config.FROM_EMAIL
    msg['To'] = to_email
    msg['Subject'] = subject

    msg.attach(MIMEText(html_body, 'html'))

    port = config.SMTP_PORT

    try:
        with smtplib.SMTP(config.SMTP_HOST, port) as server:
            server.set_debuglevel(0)  # Set to 1 for verbose SMTP debugging
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"   [ERROR] SMTP Authentication failed: {e}")
        print(f"   [INFO] Check that SMTP_USER is your Brevo account email and SMTP_PASS is your SMTP API key (not account password)")
        return False
    except smtplib.SMTPException as e:
        print(f"   [ERROR] SMTP error: {e}")
        return False
    except Exception as e:
        print(f"   [ERROR] Unexpected error sending email: {e}")
        return False

def process_subscription(sub_id, trigger="cron", conn=None, dry_run=False):
    """Sends the next delivery for one subscription. Returns (ok, message).

    trigger "cron" honours the reader's delivery hour and the once-a-day rule;
    "next" is the button in the email and is rate limited instead.
    dry_run builds the email without sending it.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = db.get_db()

    try:
        sub = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        if not sub:
            return False, "Subscription not found"
        # 'completed' is allowed so a victory email can be re-sent if needed.
        if sub["status"] not in ("active", "completed"):
            return False, f"Subscription is {sub['status']}, not active"

        manager = UserManager(conn)
        user = manager.get_user(sub["user_id"])
        if not user:
            return False, "User not found"

        if trigger == "next":
            last_sent = parse_ts(sub["last_sent"])
            if last_sent:
                waited = datetime.now(pytz.utc) - last_sent
                if waited < timedelta(minutes=NEXT_COOLDOWN_MINUTES):
                    left = NEXT_COOLDOWN_MINUTES - int(waited.total_seconds() / 60)
                    return False, f"Please wait {left} more minutes before the next part."

        if trigger == "cron":
            try:
                user_tz = pytz.timezone(user["timezone"] or "UTC")
            except pytz.UnknownTimeZoneError:
                user_tz = pytz.UTC

            now_local = datetime.now(user_tz)
            if now_local.hour != sub["delivery_hour"]:
                return False, (f"Not delivery time (local hour {now_local.hour}, "
                               f"target {sub['delivery_hour']})")

            last_sent = parse_ts(sub["last_sent"])
            if last_sent and last_sent.astimezone(user_tz).date() == now_local.date():
                return False, "Already sent today."

        email = manager.email_for(sub["user_id"])
        if not email:
            return False, "Could not decrypt the subscriber's address"

        book = conn.execute("SELECT * FROM books WHERE id = ?", (sub["book_id"],)).fetchone()
        if not book:
            return False, f"Book '{sub['book_id']}' is not in the library"

        first_seq = sub["current_sequence"]
        rows = conn.execute(
            """SELECT sequence, content FROM chunks
               WHERE book_id = ? AND sequence >= ? AND sequence < ?
               ORDER BY sequence""",
            (sub["book_id"], first_seq, first_seq + sub["chunks_per_day"]),
        ).fetchall()

        # Out of chunks means they finished the book.
        if not rows:
            return send_victory(conn, sub, book, email, dry_run=dry_run)

        last_seq = rows[-1]["sequence"]
        content = "\n\n".join(row["content"] for row in rows)
        total_chunks = book["total_chunks"] or last_seq

        # The recap is already in the database: chunk N's recap covers the
        # story through chunk N, so the one before this delivery is the
        # "previously" text, whatever chunks_per_day happens to be.
        recap = None
        if first_seq > 1:
            previous = conn.execute(
                "SELECT recap FROM chunks WHERE book_id = ? AND sequence = ?",
                (sub["book_id"], first_seq - 1),
            ).fetchone()
            recap = previous["recap"] if previous else None

        part_label = (f"Part {first_seq}" if first_seq == last_seq
                      else f"Parts {first_seq}-{last_seq}")
        subject = f"dailyLitBits: {book['title']} ({part_label} of {total_chunks})"
        html_body = format_email_html(
            book["title"], first_seq, last_seq, total_chunks, content,
            security.generate_unsub_token(sub["id"]),
            security.generate_next_token(sub["id"]),
            security.generate_dashboard_token(sub["user_id"]),
            recap,
        )

        if dry_run:
            return True, (f"[dry run] {subject} - {len(rows)} chunk(s), "
                          f"{len(html_body)} bytes, recap {'yes' if recap else 'no'}")

        if not send_via_brevo(email, subject, html_body):
            return False, "Failed to send email"

        conn.execute(
            "UPDATE subscriptions SET current_sequence = ?, last_sent = ? WHERE id = ?",
            (last_seq + 1, utc_now(), sub["id"]),
        )
        return True, f"Sent {part_label} of {total_chunks}."
    finally:
        if owns_conn:
            conn.close()


def send_victory(conn, sub, book, email, dry_run=False):
    """The 'you finished it' email: stats, what to read next, queue handover."""
    started = parse_ts(sub["created_at"]) or datetime.now(pytz.utc)
    days_taken = max((datetime.now(pytz.utc) - started).days, 1)

    # Don't recommend anything they have already finished.
    read_ids = [r["book_id"] for r in conn.execute(
        "SELECT book_id FROM subscriptions WHERE user_id = ? AND status = 'completed'",
        (sub["user_id"],))]
    read_ids.append(sub["book_id"])
    holes = ",".join("?" * len(read_ids))

    available = conn.execute(
        f"SELECT id, title, author FROM books WHERE id NOT IN ({holes}) ORDER BY title",
        read_ids).fetchall()
    read_titles = [r["title"] for r in conn.execute(
        f"SELECT title FROM books WHERE id IN ({holes})", read_ids)]

    suggestions = []
    if available:
        catalog = [{"id": r["id"], "title": r["title"], "author": r["author"] or "Unknown"}
                   for r in available]
        picked = ai.get_recommendations(read_titles, catalog)
        if picked:
            spots = ",".join("?" * len(picked))
            # dicts, not sqlite3.Row: format_victory_email uses .get()
            suggestions = [dict(row) for row in conn.execute(
                f"SELECT id, title, author FROM books WHERE id IN ({spots})", picked)]
        if not suggestions:
            print("   [Victory] No AI picks; falling back to a random three.")
            suggestions = [dict(row) for row in random.sample(available, min(3, len(available)))]

    # Promote whatever they queued next.
    next_sub = conn.execute(
        """SELECT * FROM subscriptions WHERE user_id = ? AND status = 'queued'
           ORDER BY created_at LIMIT 1""", (sub["user_id"],)).fetchone()

    next_title = None
    if next_sub:
        conn.execute(
            """UPDATE subscriptions
               SET status = 'active', current_sequence = 1, created_at = ?, last_sent = NULL
               WHERE id = ?""", (utc_now(), next_sub["id"]))
        row = conn.execute("SELECT title FROM books WHERE id = ?",
                           (next_sub["book_id"],)).fetchone()
        next_title = row["title"] if row else None

    message = f"Congratulations! You have finished <strong>{book['title']}</strong>."
    if next_title:
        message += (f"<p style=\"margin-top: 20px;\">Your next book, "
                    f"<strong>{next_title}</strong>, starts arriving tomorrow!</p>")

    subject = f"You finished {book['title']}!"
    html_body = format_victory_email(
        book["title"], days_taken, book["total_words"], suggestions,
        security.generate_dashboard_token(sub["user_id"]), message)

    if dry_run:
        return True, (f"[dry run] {subject} - {len(suggestions)} suggestion(s)"
                      + (f", queued '{next_title}' promoted" if next_title else ""))

    if not send_via_brevo(email, subject, html_body):
        return False, "Failed to send victory email"

    conn.execute("UPDATE subscriptions SET status = 'completed', last_sent = ? WHERE id = ?",
                 (utc_now(), sub["id"]))
    return True, "Victory email sent."


def run_cron(conn, dry_run=False):
    """Every active subscription gets asked; most answer 'not my hour'."""
    print("Running dispatch.py in cron mode...")
    rows = conn.execute("SELECT id FROM subscriptions WHERE status = 'active'").fetchall()

    if not rows:
        print("   No active subscriptions found.")
        return

    sent = 0
    for row in rows:
        ok, message = process_subscription(row["id"], trigger="cron", conn=conn,
                                           dry_run=dry_run)
        if ok:
            sent += 1
            print(f"   [{row['id']}] {message}")
        else:
            print(f"   [{row['id']}] skipped: {message}")

    print(f"--- RUN COMPLETE. Sent {sent} emails. ---")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send dailyLitBits deliveries")
    parser.add_argument("mode", nargs="?", default="cron", choices=["cron", "force"],
                        help="cron (default) walks every active subscription; "
                             "force sends one now, ignoring the delivery hour")
    parser.add_argument("sub_id", nargs="?", type=int, help="subscription id, for force")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the email but do not send it")
    args = parser.parse_args()

    conn = db.get_db()
    try:
        if args.mode == "force":
            if not args.sub_id:
                parser.error("force mode needs a subscription id")
            ok, message = process_subscription(args.sub_id, trigger="next", conn=conn,
                                               dry_run=args.dry_run)
            print(("OK: " if ok else "FAILED: ") + message)
        else:
            run_cron(conn, dry_run=args.dry_run)
    finally:
        conn.close()
