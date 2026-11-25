"""dailyLitBits web app.

Identity is one thing only: a signed link mailed to an email address. There is
no username, no password and no "claimed" account. `users.role` decides who is
an admin, so /admin is a permission on the ordinary user row rather than a
second login mechanism (it does want a fresh, short-lived token - see
security.py for why).

Data lives in SQLite (see db.py). Several queries alias columns to the names
the templates already use (id AS book_id, blurb AS description) rather than
churn the templates for a rename.
"""
from logger import log
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import html
import ipaddress

import requests
import db
import dispatch
import events
import reading_queue
import security
import stats
from security import verify_unsub_token, verify_next_token, verify_dashboard_token
from user_manager import UserManager

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
# The signup form shows the Turnstile widget only when a site key is set.
templates.env.globals["turnstile_site_key"] = config.TURNSTILE_SITE_KEY

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def client_ip(request):
    """The requester's address. nginx passes it as X-Real-IP; the header is only
    believed when the request arrived from a private address (the proxy), so a
    request that somehow reaches the app directly cannot choose its own."""
    peer = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(peer)
        via_proxy = address.is_private or address.is_loopback
    except ValueError:
        via_proxy = False
    if via_proxy:
        forwarded = request.headers.get("x-real-ip", "").strip()
        if forwarded:
            return forwarded
    return peer


def turnstile_passes(token, ip):
    """Server-side check of the Turnstile widget's token. Fails closed: no
    secret, no token, a network error or an odd answer all mean no."""
    if not config.TURNSTILE_SECRET_KEY or not token:
        return False
    try:
        response = requests.post(TURNSTILE_VERIFY_URL, timeout=8, data={
            "secret": config.TURNSTILE_SECRET_KEY, "response": token, "remoteip": ip})
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        log(f"[WARN] Turnstile check could not complete: {type(exc).__name__}")
        return False
    if not result.get("success"):
        log(f"[INFO] Turnstile refused a signup: {result.get('error-codes')}")
        return False
    hostname = (result.get("hostname") or "").lower()
    if config.TURNSTILE_HOSTNAMES and hostname not in config.TURNSTILE_HOSTNAMES:
        log(f"[WARN] Turnstile token was for another hostname: {hostname}")
        return False
    return True

BASE_URL = "https://dailylitbits.com"

# Create the schema once at start-up, not on every request.
_startup_conn = db.connect()
db.init_db(_startup_conn)
_startup_conn.close()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def message_page(heading, body=None, action_url=None, action_label=None, status=400):
    """A short page with one way forward.

    Rendered without a Request, so it can be returned from a handler that has
    none, and with signed_in forced false: these are the pages you land on
    when we could not work out who you are, and a nav offering "My dashboard"
    there would be a lie.
    """
    html = templates.get_template("message.html").render(
        heading=heading, body=body,
        action_url=action_url, action_label=action_label,
        signed_in=False,
    )
    return HTMLResponse(html, status_code=status)


def invalid_link(message="Invalid or expired link"):
    """Where a dead link lands: a lapsed session, a mistyped token, a link
    meant for another address. Signing out and then returning to a bookmarked
    /profile arrives here too, which is why it offers a fresh link rather than
    a bare sentence."""
    return message_page(
        message,
        "This link may have expired, been mistyped, or been meant for a "
        "different address. We can send a fresh one to your inbox.",
        action_url="/login",
        action_label="Email me a new link",
    )


def simple_page(heading, body, colour="#2c3e50"):
    """The small confirmation pages. Matches the look of the originals."""
    return HTMLResponse(f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px; color: #333;">
        <div style="max-width: 500px; margin: 0 auto;">
            <h1 style="color: {colour};">{heading}</h1>
            {body}
            <p style="margin-top: 30px;"><a href="/" style="color: #666;">Return to DailyLitBits</a></p>
        </div>
    </body>
    </html>
    """)


def user_from_token(conn, token):
    """Resolves a dashboard token to a user row, or None.

    Takes None without complaint: a visitor carrying neither a link nor a
    session is now an ordinary case, not a malformed request.
    """
    if not token:
        return None

    user_id = verify_dashboard_token(token)
    if not user_id:
        return None
    return UserManager(conn).get_user(int(user_id))


def owned_subscription(conn, sub_id, user_id):
    """A subscription, but only if it belongs to this user."""
    return conn.execute(
        "SELECT * FROM subscriptions WHERE id = ? AND user_id = ?", (sub_id, user_id)
    ).fetchone()


# --- SESSION ---
#
# The signed dashboard token IS the credential (see security.py), and until now
# the only place it ever lived was the address bar. Clicking "Library" from the
# dashboard dropped it, so the next page had no idea who the reader was.
#
# So we hand the same token to the browser as a cookie and let the browser
# carry it. No session table and no second secret: a cookie is checked by the
# same verify_dashboard_token() a URL token goes through.
SESSION_COOKIE = "dlb_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # thirty days, then it lapses on its own


def set_session(response, token):
    """Hands the token to the browser.

    httponly: page scripts cannot read it, so an injected script cannot lift it.
    secure:   never sent over plain http.
    samesite: not sent when another site posts a form at us, which is what
              keeps the dashboard's own forms safe now that a cookie can
              identify their sender.
    """
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


def clear_session(response):
    # An empty value with no lifetime, rather than delete_cookie, so the same
    # flags go out on the way back - a cookie is only replaced by one whose
    # name, path and flags match.
    response.set_cookie(
        SESSION_COOKIE, "", max_age=0, httponly=True, secure=True, samesite="lax"
    )
    return response


def session_token(request, token=None):
    """This request's token: the URL's if it has one, otherwise the cookie's.

    A mailed link wins deliberately. A reader opening a fresh link in a browser
    that already holds a session gets the identity they just clicked, which is
    what a household sharing a laptop would expect.
    """
    return token or request.cookies.get(SESSION_COOKIE)


def user_from_request(conn, request, token=None):
    return user_from_token(conn, session_token(request, token))


def signed_in(request):
    """Whether the nav should offer a dashboard rather than a way in.

    Signature only, no database: this is asked on every page, and it decides
    which link to draw, not what anyone is allowed to reach.
    """
    return bool(verify_dashboard_token(request.cookies.get(SESSION_COOKIE) or ""))


# --- WHAT A READER ALREADY HAS ---

BOOK_STATE_LABELS = {
    "active": "Reading now",
    "paused": "Paused",
    "queued": "In your queue",
    "completed": "Finished",
}


def book_states(conn, user_id):
    """This reader's standing with each book, keyed by book id.

    One entry per book, favouring the live subscription: an old completed row
    can sit beside a fresh one for the same book, and "Reading now" is the
    truer label of the two.
    """
    rows = conn.execute(
        """SELECT book_id, status FROM subscriptions
           WHERE user_id = ? AND status IN ('active', 'paused', 'queued', 'completed')
           ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1
                                WHEN 'queued' THEN 2 ELSE 3 END""",
        (user_id,),
    ).fetchall()

    states = {}
    for row in rows:
        states.setdefault(row["book_id"], row["status"])
    return states


def library_notice(conn, started=None, queued=None, already=None):
    """The line confirming a queue click, which travels back through the URL.

    A redirect keeps nothing the handler knew, so the outcome rides as a book
    id and the title is looked up again here.
    """
    for book_id, sentence in ((started, "'{title}' starts arriving tomorrow morning."),
                              (queued, "'{title}' is in your queue."),
                              (already, "'{title}' is already on your list.")):
        if not book_id:
            continue
        row = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        if row:
            return sentence.format(title=row["title"])
    return None


def with_queue_ends(rows):
    """Mark the first and last book in the queue, so the page can draw the
    arrows that would do nothing as disabled rather than as a trap.

    Paused books sit in the same list but are not in the queue, so they do not
    count towards either end.
    """
    books = [dict(row) for row in rows]
    in_line = [book for book in books if book["status"] == "queued"]

    for position, book in enumerate(in_line):
        book["first_in_queue"] = position == 0
        book["last_in_queue"] = position == len(in_line) - 1

    return books


def book_choices(conn):
    return conn.execute(
        "SELECT id AS book_id, title, author FROM books ORDER BY title"
    ).fetchall()


def cover_url_for(book):
    """The cover downloaded at ingest, else Gutenberg's own, else nothing.

    Returning None rather than a placeholder URL lets the template draw its own
    title plate, which is one fewer third-party image for a page to depend on.
    """
    if book.get("cover_path"):
        return book["cover_path"]

    clean_id = str(book["book_id"]).replace("pg", "")
    if clean_id.isdigit():
        return (f"https://www.gutenberg.org/cache/epub/{clean_id}"
                f"/pg{clean_id}.cover.medium.jpg")
    return None


def added_label(created_at):
    """'28 Oct' for a stored timestamp, or '' when it cannot be read.

    A book row always has created_at, but a hand-inserted one may not parse,
    and a date that cannot be read is no reason to lose the whole section.
    """
    try:
        return datetime.fromisoformat(created_at).strftime("%-d %b")
    except (TypeError, ValueError):
        return ""


def recent_books(conn, limit=4):
    """The books most recently added to the library, newest first.

    Ties break on id so the order is stable: the rebuild loaded most of the
    library within the same few seconds, and an unstable sort there would
    reshuffle the home page on every request.
    """
    rows = conn.execute(
        """SELECT id AS book_id, title, author, cover_path, created_at
           FROM books ORDER BY created_at DESC, id DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    books = []
    for row in rows:
        book = dict(row)
        book["cover_url"] = cover_url_for(book)
        book["added"] = added_label(book.get("created_at"))
        books.append(book)
    return books


# --- EMAILS ---

def send_confirmation_email(to_email, book_title, confirm_link):
    """The only mail an unconfirmed address ever gets. Deliberately plain: no
    book text, no dashboard link, nothing that works without the click."""
    title = html.escape(book_title)
    subject = f"Confirm your dailyLitBits subscription: {book_title}"
    body = f"""
    <html>
    <body style="font-family: sans-serif; padding: 20px; background-color: #ffffff; color: #232019;">
        <h2 style="color: #2c3e50;">Please confirm your subscription</h2>
        <p>Someone, hopefully you, asked dailyLitBits to send <strong>{title}</strong>
        to this address: a few hundred words of the book each morning, by email.</p>
        <p><a href="{confirm_link}" style="background-color: #1f6b4f; color: white; padding: 10px 15px; text-decoration: none; border-radius: 5px;">Confirm and start my book</a></p>
        <p style="color: #777; font-size: 12px;">If this wasn't you, ignore this email. Nothing else
        will be sent, and the request is forgotten after a week. The link works for 48 hours.</p>
    </body>
    </html>
    """
    if dispatch.send_via_brevo(to_email, subject, body):
        log("[INFO] Confirmation email sent")
    else:
        log("[ERROR] Failed to send confirmation email")


def send_welcome_email(to_email, book_title, dashboard_link, is_queue=False):
    if is_queue:
        subject = f"Added to Queue: {book_title}"
        intro = (f"<h2 style=\"color: #2c3e50;\">You're in the queue!</h2>"
                 f"<p><strong>{book_title}</strong> has been added to your reading list.</p>"
                 f"<p>It will start automatically once you finish your current book.</p>")
    else:
        subject = f"Welcome to dailyLitBits: {book_title}"
        intro = (f"<h2 style=\"color: #2c3e50;\">Welcome to dailyLitBits!</h2>"
                 f"<p>You have successfully subscribed to <strong>{book_title}</strong>.</p>"
                 f"<p>Your first part arrives tomorrow morning.</p>")

    body = f"""
    <html>
    <body style="font-family: sans-serif; padding: 20px;">
        {intro}
        <p>Use your personal link to manage your subscription, change how much
        arrives each day, or read ahead:</p>
        <p><a href="{dashboard_link}" style="background-color: #28a745; color: white; padding: 10px 15px; text-decoration: none; border-radius: 5px;">Open my dashboard</a></p>
        <br>
        <p style="color: #777; font-size: 12px;">Keep this email - the link is how you get back in.
        You can always have it sent again from the site.</p>
    </body>
    </html>
    """

    if dispatch.send_via_brevo(to_email, subject, body):
        log("[INFO] Welcome email sent")
    else:
        log("[ERROR] Failed to send welcome email")


def send_dashboard_link(to_email, dashboard_link):
    body = f"""
    <html>
    <body style="font-family: sans-serif; padding: 20px;">
        <h2 style="color: #2c3e50;">Your dashboard link</h2>
        <p>Here is your way back in:</p>
        <p><a href="{dashboard_link}" style="background-color: #28a745; color: white; padding: 10px 15px; text-decoration: none; border-radius: 5px;">Open my dashboard</a></p>
        <br>
        <p style="color: #777; font-size: 12px;">If you did not ask for this, you can ignore it.</p>
    </body>
    </html>
    """
    return dispatch.send_via_brevo(to_email, "Your dailyLitBits dashboard link", body)


# --- PUBLIC PAGES ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, conn=Depends(get_db)):
    return templates.TemplateResponse("index.html", {
        "request": request, "signed_in": signed_in(request), "books": book_choices(conn), "recent": recent_books(conn)
    })


@app.get("/library", response_class=HTMLResponse)
async def library(request: Request, started: str = None, queued: str = None,
                  already: str = None, conn=Depends(get_db)):
    rows = conn.execute(
        """SELECT id AS book_id, title, author, total_chunks,
                  blurb AS description, cover_path
           FROM books ORDER BY title"""
    ).fetchall()

    books = []
    for row in rows:
        book = dict(row)
        book["cover_url"] = cover_url_for(book)
        books.append(book)

    # Signed in, the library stops being a catalogue and becomes the place a
    # reader manages their own list, so it needs to know who is looking.
    user = user_from_request(conn, request)

    return templates.TemplateResponse("library.html", {
        "request": request, "signed_in": signed_in(request),
        "books": books,
        "token": session_token(request) if user else None,
        "book_states": book_states(conn, user["id"]) if user else {},
        "state_labels": BOOK_STATE_LABELS,
        "notice": library_notice(conn, started=started, queued=queued, already=already),
    })


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request, "signed_in": signed_in(request)})


@app.get("/intro", response_class=HTMLResponse)
async def intro_page(request: Request):
    return templates.TemplateResponse("intro.html", {"request": request, "signed_in": signed_in(request)})


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, conn=Depends(get_db)):
    """The public numbers. See stats.py for which of them involve a reader."""
    return templates.TemplateResponse("stats.html", {
        "request": request, "signed_in": signed_in(request),
        **stats.public_stats(conn),
    })


# --- SIGNUP ---

def signup_test_allowed(email):
    """True for an address on config.SIGNUP_TEST_ADDRESSES, or a plus-alias of
    one (name+anything@domain), so a tester can sign up repeatedly as new."""
    address = email.strip().lower()
    local, at, domain = address.partition("@")
    if not at or not domain:
        return False
    base = f"{local.split('+', 1)[0]}@{domain}"
    return address in config.SIGNUP_TEST_ADDRESSES or base in config.SIGNUP_TEST_ADDRESSES


# What every accepted-looking signup says, whether or not mail went out, so the
# form does not reveal limits, suppressions or the honeypot.
SIGNUP_CHECK_EMAIL = ("Almost there: we've sent a confirmation link to that address. "
                      "Nothing else will arrive unless it is clicked.")


@app.post("/signup", response_class=HTMLResponse)
async def handle_signup(
    request: Request,
    email: str = Form(...),
    book_id: str = Form(""),
    timezone_name: str = Form("UTC", alias="timezone"),
    chunks_per_day: int = Form(2),
    turnstile_token: str = Form("", alias="cf-turnstile-response"),
    honeypot: str = Form("", alias="website"),
    conn=Depends(get_db),
):
    if not config.SIGNUPS_OPEN and not signup_test_allowed(email):
        return templates.TemplateResponse("index.html", {
            "request": request, "signed_in": signed_in(request), "books": book_choices(conn), "recent": recent_books(conn),
            "message": "New signups are paused for a few days while we improve the service. Please check back soon.",
        })

    # A person never sees the honeypot field; a bot filling every input does.
    # It gets the ordinary answer and nothing happens. One signal among
    # several: Turnstile and the rate limits still apply to everything else.
    if honeypot.strip():
        log("[WARN] Signup dropped: honeypot field was filled")
        return templates.TemplateResponse("index.html", {
            "request": request, "signed_in": signed_in(request), "books": book_choices(conn), "recent": recent_books(conn),
            "message": SIGNUP_CHECK_EMAIL,
        })

    if not book_id.strip():
        return templates.TemplateResponse("index.html", {
            "request": request, "signed_in": signed_in(request), "books": book_choices(conn), "recent": recent_books(conn),
            "message": "Please choose a book to start with.",
        })

    book = conn.execute("SELECT id, title FROM books WHERE id = ?", (book_id,)).fetchone()
    if not book:
        return templates.TemplateResponse("index.html", {
            "request": request, "signed_in": signed_in(request), "books": book_choices(conn), "recent": recent_books(conn),
            "message": "That book is not in the library.",
        })

    chunks_per_day = max(1, min(chunks_per_day, 12))
    manager = UserManager(conn)
    lookup = manager.email_lookup(email)
    now = datetime.now(timezone.utc)

    def stamp(moment):
        return moment.isoformat(timespec="seconds")

    def page(message):
        return templates.TemplateResponse("index.html", {
            "request": request, "signed_in": signed_in(request), "books": book_choices(conn), "recent": recent_books(conn),
            "message": message,
        })

    # The same answer whether or not a confirmation actually goes out, so the
    # form cannot be used to learn which addresses are already readers.
    check_email = SIGNUP_CHECK_EMAIL

    ip = client_ip(request)
    if not turnstile_passes(turnstile_token, ip):
        return page("Please complete the human check below the form and try again.")
    ip_key = security.ip_hash(ip)

    pending_id = None
    with db.transaction(conn):
        # Per client address, before anything else: one source cannot fill the
        # site-wide hourly allowance on its own.
        from_ip = conn.execute(
            """SELECT SUM(created_at >= ?), COUNT(*) FROM pending_signups
               WHERE ip_hash = ? AND created_at >= ?""",
            (stamp(now - timedelta(hours=1)), ip_key, stamp(now - timedelta(hours=24)))).fetchone()
        last_hour, last_day = (from_ip[0] or 0), from_ip[1]
        if (last_hour >= config.SIGNUPS_PER_IP_PER_HOUR
                or last_day >= config.SIGNUPS_PER_IP_PER_DAY):
            log(f"[WARN] Signup refused: per-address limit ({last_hour}/h, {last_day}/day)")
            return page("Too many signups from your network. Please try again later.")

        # Abandoned requests are kept a week, then forgotten.
        conn.execute(
            "DELETE FROM pending_signups WHERE confirmed_at IS NULL AND created_at < ?",
            (stamp(now - timedelta(days=config.PENDING_RETENTION_DAYS)),))

        # A suppressed address gets no confirmation, and the page says the
        # same as always, so the form does not reveal who is on the list.
        if conn.execute("SELECT 1 FROM suppressions WHERE email_lookup = ?",
                        (lookup,)).fetchone():
            log("[INFO] Signup for a suppressed address: nothing sent")
            return page(check_email)

        # One unconfirmed request per address per day, so the form cannot be
        # used to bury someone in confirmations.
        waiting = conn.execute(
            """SELECT 1 FROM pending_signups
               WHERE email_lookup = ? AND confirmed_at IS NULL AND created_at >= ?""",
            (lookup, stamp(now - timedelta(hours=24)))).fetchone()
        if not waiting:
            recent = conn.execute(
                "SELECT COUNT(*) FROM pending_signups WHERE created_at >= ?",
                (stamp(now - timedelta(hours=1)),)).fetchone()[0]
            if recent >= config.CONFIRM_SENDS_PER_HOUR:
                log(f"[WARN] Signup refused: {recent} confirmation requests in the last hour")
                return page("We're getting a lot of signups right now. Please try again in an hour.")
            pending_id = conn.execute(
                """INSERT INTO pending_signups
                       (email_enc, email_lookup, book_id, timezone, chunks_per_day,
                        created_at, ip_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (manager.encrypt_email(email), lookup, book_id, timezone_name,
                 chunks_per_day, stamp(now), ip_key)).lastrowid

    if pending_id:
        token = security.generate_confirm_token(pending_id)
        send_confirmation_email(email, book["title"], f"{BASE_URL}/confirm?token={token}")
    return page(check_email)


def create_subscription(conn, manager, email, book, timezone_name, chunks_per_day):
    """Makes the reader and the subscription once an address is confirmed.
    Returns (user_id, status, refusal). refusal is a sentence explaining a
    duplicate, and in that case nothing was written."""
    user_id, _ = manager.get_or_create_user(email, timezone_name=timezone_name)

    existing = conn.execute(
        """SELECT status FROM subscriptions
           WHERE user_id = ? AND book_id = ?
             AND status IN ('active', 'queued', 'paused', 'completed')""",
        (user_id, book["id"]),
    ).fetchone()
    if existing:
        return user_id, None, {
            "active": f"You are already reading '{book['title']}'.",
            "paused": f"'{book['title']}' is paused - resume it from your dashboard.",
            "queued": f"'{book['title']}' is already in your queue.",
            "completed": f"You have already finished '{book['title']}'.",
        }[existing["status"]]

    # First book starts now; anything after it waits its turn.
    active_count = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchone()[0]
    status = "active" if active_count == 0 else "queued"

    position = (reading_queue.back_position(conn, user_id)
                if status == "queued" else None)
    cur = conn.execute(
        """INSERT INTO subscriptions
               (user_id, book_id, status, current_sequence, chunks_per_day,
                delivery_hour, queue_position, last_sent, created_at)
           VALUES (?, ?, ?, 1, ?, ?, ?, NULL, ?)""",
        (user_id, book["id"], status, chunks_per_day, dispatch.TARGET_HOUR,
         position, utc_now()),
    )
    events.record(conn, "subscribed", subscription_id=cur.lastrowid,
                  book_id=book["id"], sequence=1, status=status,
                  chunks_per_day=chunks_per_day)
    return user_id, status, None


def pending_from_token(conn, token):
    pending_id = security.verify_confirm_token(token)
    if not pending_id:
        return None
    return conn.execute(
        """SELECT p.*, b.title FROM pending_signups p JOIN books b ON b.id = p.book_id
           WHERE p.id = ?""", (int(pending_id),)).fetchone()


CONFIRM_LINK_DEAD = "This confirmation link is invalid or has expired. Please sign up again."
ALREADY_CONFIRMED = ("Already confirmed",
                     "<p>This address is already confirmed. Your welcome email has the "
                     "dashboard link for managing your books.</p>")


@app.get("/confirm", response_class=HTMLResponse)
async def confirm_signup_page(token: str, conn=Depends(get_db)):
    """Only asks. Link scanners follow GETs; a person presses the button."""
    pending = pending_from_token(conn, token)
    if not pending:
        return invalid_link(CONFIRM_LINK_DEAD)
    if pending["confirmed_at"]:
        return simple_page(*ALREADY_CONFIRMED)
    return confirm_page(
        "Confirm your subscription",
        f"Start sending <strong>{html.escape(pending['title'])}</strong> to this address, "
        f"a few hundred words each morning?",
        "/confirm", {"token": token}, "Yes, start my book")


@app.post("/confirm", response_class=HTMLResponse)
async def confirm_signup(token: str = Form(...), conn=Depends(get_db)):
    pending = pending_from_token(conn, token)
    if not pending:
        return invalid_link(CONFIRM_LINK_DEAD)

    manager = UserManager(conn)
    email = manager.decrypt_email(pending["email_enc"])
    book = {"id": pending["book_id"], "title": pending["title"]}
    if conn.execute("SELECT 1 FROM suppressions WHERE email_lookup = ?",
                    (manager.email_lookup(email),)).fetchone():
        return simple_page("We can't send to this address",
                           "<p>Email to this address has bounced or been declined, "
                           "so we can't start a subscription for it.</p>",
                           colour="#dc3545")
    user_id = status = refusal = None
    try:
        with db.transaction(conn):
            # The claim and the subscription commit together, so a failure
            # leaves the link usable rather than half-spent.
            claimed = conn.execute(
                "UPDATE pending_signups SET confirmed_at = ? WHERE id = ? AND confirmed_at IS NULL",
                (utc_now(), pending["id"])).rowcount
            if claimed:
                user_id, status, refusal = create_subscription(
                    conn, manager, email, book, pending["timezone"], pending["chunks_per_day"])
    except Exception as exc:
        log(f"[ERROR] Confirmation failed: {type(exc).__name__}: {exc}")
        return simple_page("Something went wrong",
                           "<p>We couldn't finish confirming. Please try the link again.</p>",
                           colour="#dc3545")

    if not claimed:
        return simple_page(*ALREADY_CONFIRMED)
    if refusal:
        return simple_page("Confirmed",
                           f"<p>Thanks, your address is confirmed.</p><p>{html.escape(refusal)}</p>",
                           colour="#28a745")

    dashboard = security.generate_dashboard_token(user_id)
    send_welcome_email(email, book["title"], f"{BASE_URL}/profile?token={dashboard}",
                       is_queue=(status == "queued"))
    message = (f"Added '{book['title']}' to your queue. Check your email for your dashboard link."
               if status == "queued" else
               f"Success! '{book['title']}' starts tomorrow. Check your email for your dashboard link.")
    return simple_page("You're confirmed!",
                       f"<p style=\"font-size: 18px;\">{html.escape(message)}</p>",
                       colour="#28a745")


class _AlreadySubscribed(Exception):
    """Signals a duplicate subscription so the transaction unwinds cleanly."""


# --- GETTING BACK IN (replaces username/password login) ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "signed_in": signed_in(request)})


@app.post("/login", response_class=HTMLResponse)
async def handle_login(request: Request, email: str = Form(...), conn=Depends(get_db)):
    user = UserManager(conn).get_user_by_email(email)

    # Always say the same thing: whether an address is registered is not
    # something a stranger should be able to probe.
    if user:
        send_dashboard_link(email, f"{BASE_URL}/profile?token="
                                   f"{security.generate_dashboard_token(user['id'])}")

    return templates.TemplateResponse("login.html", {
        "request": request, "signed_in": signed_in(request),
        "message": "If that address has a subscription, its dashboard link is on its way.",
    })


@app.post("/logout")
async def logout():
    """Forgets the browser. Worth having on a shared machine - and it locks
    nobody out, because the mailed link still works."""
    return clear_session(RedirectResponse(url="/", status_code=303))


# --- DASHBOARD ---

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, token: str = None, conn=Depends(get_db)):
    user = user_from_request(conn, request, token)
    if not user:
        return invalid_link()

    # A token that arrived in the URL becomes the session, and then the URL is
    # swapped for a bare /profile. The credential stops being left behind in
    # browser history, in a screenshot of the page, or in the referrer sent to
    # whoever hosts a cover image.
    if token:
        return set_session(RedirectResponse(url="/profile", status_code=303), token)

    token = session_token(request)

    current = conn.execute(
        """SELECT * FROM subscriptions WHERE user_id = ?
             AND status IN ('active', 'paused')
           ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at LIMIT 1""",
        (user["id"],),
    ).fetchone()

    # Everything the reader has on the go but is not currently receiving.
    # Paused subscriptions MUST appear here: a paused book sends no email, so
    # the dashboard is the only place its reader can reach it. Listing only
    # 'queued' left a paused book invisible whenever another was active, with
    # its reading position stranded in the database.
    others = conn.execute(
        """SELECT s.id AS subscription_id, s.status, s.current_sequence,
                  b.id AS book_id, b.title, b.author, b.total_chunks
           FROM subscriptions s JOIN books b ON b.id = s.book_id
           WHERE s.user_id = ? AND s.status IN ('queued', 'paused') AND s.id != ?
           ORDER BY CASE s.status WHEN 'paused' THEN 0 ELSE 1 END,
                    s.queue_position, s.created_at""",
        (user["id"], current["id"] if current else -1),
    ).fetchall()

    # NB: `current` is resolved above, because `others` excludes it by id.
    context = {
        "request": request, "signed_in": signed_in(request),
        "token": token,
        "user_tz": user["timezone"],
        "is_admin": user["role"] == "admin",
        "other_books": with_queue_ends(others),
        "sub_id": None,
        "book_title": None,
    }

    if current:
        book = conn.execute("SELECT * FROM books WHERE id = ?",
                            (current["book_id"],)).fetchone()
        total = (book["total_chunks"] if book and book["total_chunks"] else 1)
        started = dispatch.parse_ts(current["created_at"])

        context.update({
            "sub_id": current["id"],
            "book_title": book["title"] if book else current["book_id"],
            "book_author": (book["author"] if book else None) or "Unknown",
            "current": current["current_sequence"],
            "total": total,
            "percent": min(int((current["current_sequence"] / total) * 100), 100),
            "start_date": started.strftime("%b %d, %Y") if started else "Unknown",
            "status": current["status"],
            "delivery_hour": current["delivery_hour"],
            "chunks_per_day": current["chunks_per_day"],
        })

    return templates.TemplateResponse("dashboard.html", context)


@app.post("/toggle_pause", response_class=HTMLResponse)
async def toggle_pause(token: str = Form(...), sub_id: int = Form(...), conn=Depends(get_db)):
    user = user_from_token(conn, token)
    if not user:
        return invalid_link()

    sub = owned_subscription(conn, sub_id, user["id"])
    if not sub:
        return invalid_link("Subscription not found")

    new_status = "paused" if sub["status"] == "active" else "active"
    with db.transaction(conn):
        conn.execute("UPDATE subscriptions SET status = ? WHERE id = ?", (new_status, sub_id))
        events.record(conn, "paused" if new_status == "paused" else "resumed",
                      subscription_id=sub_id, book_id=sub["book_id"],
                      sequence=sub["current_sequence"])

    return RedirectResponse(url=f"/profile?token={token}", status_code=303)


@app.post("/update_preferences", response_class=HTMLResponse)
async def update_preferences(
    token: str = Form(...),
    sub_id: int = Form(...),
    timezone_name: str = Form(..., alias="timezone"),
    delivery_hour: int = Form(...),
    chunks_per_day: int = Form(...),
    conn=Depends(get_db),
):
    user = user_from_token(conn, token)
    if not user:
        return invalid_link()

    sub = owned_subscription(conn, sub_id, user["id"])
    if not sub:
        return invalid_link("Subscription not found")

    # Changing how much arrives does NOT reset progress - that was the whole
    # point of dropping the three-edition scheme.
    with db.transaction(conn):
        UserManager(conn).set_timezone(user["id"], timezone_name)
        hour = max(0, min(delivery_hour, 23))
        parts = max(1, min(chunks_per_day, 12))
        conn.execute(
            "UPDATE subscriptions SET delivery_hour = ?, chunks_per_day = ? WHERE id = ?",
            (hour, parts, sub_id),
        )
        events.record(conn, "prefs_changed", subscription_id=sub_id,
                      book_id=sub["book_id"], sequence=sub["current_sequence"],
                      timezone=timezone_name, delivery_hour=hour, chunks_per_day=parts)

    return RedirectResponse(url=f"/profile?token={token}", status_code=303)


@app.post("/queue/move", response_class=HTMLResponse)
async def move_in_queue(token: str = Form(...), sub_id: int = Form(...),
                        direction: str = Form(...), conn=Depends(get_db)):
    """One place up or down. The token in the form is what authorises it."""
    user = user_from_token(conn, token)
    if not user:
        return invalid_link()

    with db.transaction(conn):
        reading_queue.move(conn, sub_id, user["id"], direction)

    # A book already at the end it was asked to move towards is not an error;
    # the page simply comes back the same.
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/queue/order")
async def set_queue_order(token: str = Form(...), sub_ids: list[int] = Form(...),
                          conn=Depends(get_db)):
    """A whole new order at once, which is what dragging a book produces.

    This one answers the page instead of redirecting it, because the drag has
    already moved the row on screen - a redirect would only make it flicker.
    The reply carries the order the database now holds, and the page uses that
    to blunt the arrows at the new ends. So where the ends of the queue are is
    still decided in exactly one place: here.
    """
    user = user_from_token(conn, token)
    if not user:
        return JSONResponse({"error": "That link is no longer valid."},
                            status_code=400)

    with db.transaction(conn):
        written = reading_queue.reorder(conn, user["id"], sub_ids)

    if not written:
        # The page is looking at a queue that has since changed - a book
        # removed in another tab, or one promoted overnight. Refusing and
        # reloading beats applying an order that was never about this list.
        return JSONResponse({"error": "That list has changed since it loaded."},
                            status_code=409)

    return JSONResponse(
        {"sub_ids": [row["id"] for row in reading_queue.queued(conn, user["id"])]})


@app.post("/queue/remove", response_class=HTMLResponse)
async def remove_from_queue(token: str = Form(...), sub_id: int = Form(...),
                            conn=Depends(get_db)):
    """Take a book off the list, keeping where the reader got to."""
    user = user_from_token(conn, token)
    if not user:
        return invalid_link()

    with db.transaction(conn):
        gone = reading_queue.remove(conn, sub_id, user["id"])
        if gone:
            events.record(conn, "unsubscribed", subscription_id=gone["id"],
                          book_id=gone["book_id"], sequence=gone["current_sequence"],
                          status=gone["status"])

    if not gone:
        return invalid_link("That book is not on your list")

    return RedirectResponse(url="/profile", status_code=303)


def confirm_page(heading, question, action, fields, button):
    """A GET from an email link only asks; the button POSTs. Mail scanners
    follow links but do not submit forms, so they can no longer act."""
    hidden = "".join(
        f'<input type="hidden" name="{name}" value="{html.escape(str(value), quote=True)}">'
        for name, value in fields.items())
    return simple_page(heading, f"""
        <p style="font-size: 18px;">{question}</p>
        <form action="{action}" method="post">{hidden}
            <button type="submit" style="background-color: #1f6b4f; color: white; padding: 10px 20px; border: none; border-radius: 5px; font-size: 16px; cursor: pointer;">{button}</button>
        </form>""")


@app.get("/switch_book", response_class=HTMLResponse)
async def switch_book_confirm(token: str, new_book_id: str, conn=Depends(get_db)):
    if not user_from_token(conn, token):
        return invalid_link()
    book = conn.execute("SELECT id, title FROM books WHERE id = ?", (new_book_id,)).fetchone()
    if not book:
        return HTMLResponse("<h1>Book not found</h1>", status_code=404)
    return confirm_page("Switch books?",
                        f"Start reading <strong>{html.escape(book['title'])}</strong> now?",
                        "/switch_book", {"token": token, "new_book_id": new_book_id},
                        "Yes, switch")


@app.post("/switch_book", response_class=HTMLResponse)
async def switch_book(token: str = Form(...), new_book_id: str = Form(...), conn=Depends(get_db)):
    """Make this book the one arriving now. Used by the queue and the
    suggestions in the victory email."""
    user = user_from_token(conn, token)
    if not user:
        return invalid_link()

    book = conn.execute("SELECT id, title FROM books WHERE id = ?", (new_book_id,)).fetchone()
    if not book:
        return HTMLResponse("<h1>Book not found</h1>", status_code=404)

    with db.transaction(conn):
        # Whatever they were reading steps aside, keeping its place.
        stepping_aside = conn.execute(
            """SELECT id, book_id, current_sequence FROM subscriptions
               WHERE user_id = ? AND status IN ('active', 'paused')""",
            (user["id"],),
        ).fetchall()

        # To the front, not the back: the book you just put down to start
        # another is the one you are most likely to want back first. It can be
        # moved from the dashboard either way.
        front = reading_queue.front_position(conn, user["id"])
        conn.execute(
            """UPDATE subscriptions SET status = 'queued', queue_position = ?
               WHERE user_id = ? AND status IN ('active', 'paused')""",
            (front, user["id"]),
        )

        for aside in stepping_aside:
            if aside["book_id"] != new_book_id:
                events.record(conn, "queued", subscription_id=aside["id"],
                              book_id=aside["book_id"], sequence=aside["current_sequence"])

        existing = conn.execute(
            """SELECT * FROM subscriptions WHERE user_id = ? AND book_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (user["id"], new_book_id),
        ).fetchone()

        if existing and existing["status"] in ("queued", "paused"):
            # Resume where they left off rather than restarting the book.
            conn.execute(
                "UPDATE subscriptions SET status = 'active', last_sent = NULL WHERE id = ?",
                (existing["id"],),
            )
            events.record(conn, "switched", subscription_id=existing["id"],
                          book_id=new_book_id, sequence=existing["current_sequence"],
                          to_book=new_book_id)
        else:
            fresh = conn.execute(
                """INSERT INTO subscriptions
                       (user_id, book_id, status, current_sequence, chunks_per_day,
                        delivery_hour, last_sent, created_at)
                   VALUES (?, ?, 'active', 1, 2, ?, NULL, ?)""",
                (user["id"], new_book_id, dispatch.TARGET_HOUR, utc_now()),
            )
            events.record(conn, "switched", subscription_id=fresh.lastrowid,
                          book_id=new_book_id, sequence=1, to_book=new_book_id)

    return simple_page(
        "You're switched!",
        f"<p style=\"font-size: 18px;\">You are now reading <strong>{book['title']}</strong>.</p>"
        f"<p>Your next part arrives tomorrow morning.</p>",
        colour="#28a745",
    )


@app.post("/queue_book", response_class=HTMLResponse)
async def queue_book(token: str = Form(...), book_id: str = Form(...),
                     conn=Depends(get_db)):
    """Add a book from the library in one click, with no email round-trip.

    The token comes from the form rather than from the cookie alone, and that
    is on purpose: it is what stops another site from posting this at a
    signed-in reader's account.
    """
    user = user_from_token(conn, token)
    if not user:
        return invalid_link()

    book = conn.execute("SELECT id, title FROM books WHERE id = ?", (book_id,)).fetchone()
    if not book:
        return message_page("Book not found",
                            "That book is not in the library any more.",
                            action_url="/library", action_label="Back to the library",
                            status=404)

    if book_id in book_states(conn, user["id"]):
        return RedirectResponse(url=f"/library?already={book_id}", status_code=303)

    # A reader's first book starts arriving; anything after it waits its turn,
    # at the back of the queue. The order is theirs to change from there.
    active_count = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND status = 'active'",
        (user["id"],),
    ).fetchone()[0]
    status = "active" if active_count == 0 else "queued"

    with db.transaction(conn):
        # A book taken off the list before comes back where it left off rather
        # than as a second subscription starting at part one.
        revived = reading_queue.revive(conn, user["id"], book_id, status)
        if revived:
            sub_id, sequence = revived["id"], revived["current_sequence"]
        else:
            position = (reading_queue.back_position(conn, user["id"])
                        if status == "queued" else None)
            sub_id = conn.execute(
                """INSERT INTO subscriptions
                       (user_id, book_id, status, current_sequence, chunks_per_day,
                        delivery_hour, queue_position, last_sent, created_at)
                   VALUES (?, ?, ?, 1, 2, ?, ?, NULL, ?)""",
                (user["id"], book_id, status, dispatch.TARGET_HOUR, position,
                 utc_now()),
            ).lastrowid
            sequence = 1

        events.record(conn, "subscribed", subscription_id=sub_id,
                      book_id=book_id, sequence=sequence, status=status,
                      trigger="library")

    outcome = "started" if status == "active" else "queued"
    return RedirectResponse(url=f"/library?{outcome}={book_id}", status_code=303)


# --- EMAIL LINKS (scoped to one subscription) ---

@app.get("/next", response_class=HTMLResponse)
async def next_chapter_confirm(token: str):
    if not verify_next_token(token):
        return invalid_link()
    return confirm_page("Next part?", "Send the next part of your book now?",
                        "/next", {"token": token}, "Send it now")


@app.post("/next", response_class=HTMLResponse)
async def trigger_next_chapter(token: str = Form(...), conn=Depends(get_db)):
    sub_id = verify_next_token(token)
    if not sub_id:
        return invalid_link()

    ok, message = dispatch.process_subscription(int(sub_id), trigger="next", conn=conn)
    return simple_page("On its way!" if ok else "Hold on...",
                       f"<p style=\"font-size: 18px;\">{message}</p>",
                       colour="#28a745" if ok else "#dc3545")


@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_confirm(request: Request, token: str, conn=Depends(get_db)):
    if not verify_unsub_token(token):
        return invalid_link()

    return HTMLResponse(f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
        <div style="max-width: 500px; margin: 0 auto;">
            <h1>Unsubscribe?</h1>
            <p>Are you sure you want to stop receiving this book?</p>
            <form action="/unsubscribe" method="post">
                <input type="hidden" name="token" value="{token}">
                <button type="submit" style="background-color: #dc3545; color: white; padding: 10px 20px; border: none; border-radius: 5px; font-size: 16px; cursor: pointer;">
                    Yes, Unsubscribe Me
                </button>
            </form>
            <p style="margin-top: 20px;"><a href="/">No, take me back</a></p>
        </div>
    </body>
    </html>
    """)


def unsubscribe_subscription(conn, sub_id, **detail):
    """Closes one subscription. True if it was open. Idempotent: a second call,
    or a call for a subscription that is gone, changes nothing."""
    sub = conn.execute("SELECT book_id, current_sequence FROM subscriptions WHERE id = ?",
                       (int(sub_id),)).fetchone()
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE subscriptions SET status = 'unsubscribed' "
            "WHERE id = ? AND status != 'unsubscribed'",
            (int(sub_id),),
        )
        if cur.rowcount and sub:
            events.record(conn, "unsubscribed", subscription_id=int(sub_id),
                          book_id=sub["book_id"], sequence=sub["current_sequence"], **detail)
    return bool(cur.rowcount)


@app.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_process(token: str = Form(...), conn=Depends(get_db)):
    sub_id = verify_unsub_token(token)
    if not sub_id:
        return invalid_link()

    closed = unsubscribe_subscription(conn, sub_id)
    return simple_page(
        "Unsubscribe",
        "<p>You have been unsubscribed.</p>" if closed
        else "<p>That subscription was already closed.</p>",
    )


# One-click unsubscribe (RFC 8058), the URL in each email's List-Unsubscribe
# header. The mailbox provider POSTs "List-Unsubscribe=One-Click" here on the
# reader's behalf: no confirmation step, no login, no redirect, and a 200
# whether or not the subscription was still open. A GET is a person or a link
# scanner, so it only gets the ordinary confirmation page.
@app.post("/unsubscribe/one-click")
async def unsubscribe_one_click(token: str, conn=Depends(get_db)):
    sub_id = verify_unsub_token(token)
    if not sub_id:
        return PlainTextResponse("Invalid unsubscribe link.", status_code=400)
    unsubscribe_subscription(conn, sub_id, trigger="one_click")
    return PlainTextResponse("You have been unsubscribed.")


@app.get("/unsubscribe/one-click", response_class=HTMLResponse)
async def unsubscribe_one_click_page(request: Request, token: str, conn=Depends(get_db)):
    return await unsubscribe_confirm(request, token, conn)


# --- SUGGESTIONS ---

@app.get("/suggest", response_class=HTMLResponse)
async def suggest_page(request: Request):
    return templates.TemplateResponse("suggest.html", {"request": request, "signed_in": signed_in(request)})


@app.post("/suggest", response_class=HTMLResponse)
async def handle_suggestion(
    title: str = Form(...),
    author: str = Form(...),
    link: str = Form(None),
    comments: str = Form(None),
    conn=Depends(get_db),
):
    conn.execute(
        """INSERT INTO suggestions (title, author, link, comments, status, submitted_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (title, author, link, comments, utc_now()),
    )
    return simple_page(
        "Thank You!",
        f"<p>We have received your suggestion for <strong>{title}</strong>.</p>"
        f"<p>If we add it to the library, it will appear on the homepage soon.</p>",
        colour="green",
    )


# --- ADMIN (a role on the user row, plus a fresh token) ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, token: str = "", conn=Depends(get_db)):
    user_id = security.verify_admin_token(token) if token else None
    if not user_id:
        return invalid_link("This admin link is missing or has expired. "
                            "Mint a new one with: python tools/admin.py link &lt;email&gt;")

    manager = UserManager(conn)
    if not manager.is_admin(int(user_id)):
        return HTMLResponse("<h1>Not an admin account</h1>", status_code=403)

    rows = conn.execute(
        """SELECT s.id, s.user_id, s.status, s.current_sequence, s.chunks_per_day,
                  s.last_sent, u.timezone, b.title AS book_title, b.total_chunks
           FROM subscriptions s
           JOIN users u ON u.id = s.user_id
           LEFT JOIN books b ON b.id = s.book_id
           ORDER BY s.created_at DESC"""
    ).fetchall()

    user_data = []
    for row in rows:
        total = row["total_chunks"] or 1
        last_sent = dispatch.parse_ts(row["last_sent"])
        user_data.append({
            "email": manager.email_for(row["user_id"]) or "[could not decrypt]",
            "timezone": row["timezone"],
            "book_title": row["book_title"] or "(missing book)",
            "current": row["current_sequence"],
            "total": total,
            "percent": min(int((row["current_sequence"] / total) * 100), 100),
            "status": row["status"],
            "last_sent": last_sent.strftime("%Y-%m-%d %H:%M") if last_sent else "Never",
        })

    return templates.TemplateResponse("admin.html", {"request": request, "signed_in": signed_in(request), "users": user_data})
