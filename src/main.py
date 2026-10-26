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
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import db
import dispatch
import security
from security import verify_unsub_token, verify_next_token, verify_dashboard_token
from user_manager import UserManager

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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


def invalid_link(message="Invalid or expired link"):
    return HTMLResponse(f"<h1>{message}</h1>", status_code=400)


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
    """Resolves a dashboard token to a user row, or None."""
    user_id = verify_dashboard_token(token)
    if not user_id:
        return None
    return UserManager(conn).get_user(int(user_id))


def owned_subscription(conn, sub_id, user_id):
    """A subscription, but only if it belongs to this user."""
    return conn.execute(
        "SELECT * FROM subscriptions WHERE id = ? AND user_id = ?", (sub_id, user_id)
    ).fetchone()


def book_choices(conn):
    return conn.execute(
        "SELECT id AS book_id, title, author FROM books ORDER BY title"
    ).fetchall()


# --- EMAILS ---

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
        "request": request, "books": book_choices(conn)
    })


@app.get("/library", response_class=HTMLResponse)
async def library(request: Request, conn=Depends(get_db)):
    rows = conn.execute(
        """SELECT id AS book_id, title, author, total_chunks,
                  blurb AS description, cover_path
           FROM books ORDER BY title"""
    ).fetchall()

    books = []
    for row in rows:
        book = dict(row)
        # Prefer the cover we downloaded at ingest; fall back to Gutenberg.
        if book.get("cover_path"):
            book["cover_url"] = book["cover_path"]
        else:
            clean_id = book["book_id"].replace("pg", "")
            book["cover_url"] = (
                f"https://www.gutenberg.org/cache/epub/{clean_id}/pg{clean_id}.cover.medium.jpg"
                if clean_id.isdigit() else "/static/covers/placeholder.jpg"
            )
        books.append(book)

    return templates.TemplateResponse("library.html", {"request": request, "books": books})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/intro", response_class=HTMLResponse)
async def intro_page(request: Request):
    return templates.TemplateResponse("intro.html", {"request": request})


# --- SIGNUP ---

@app.post("/signup", response_class=HTMLResponse)
async def handle_signup(
    request: Request,
    email: str = Form(...),
    book_id: str = Form(...),
    timezone_name: str = Form("UTC", alias="timezone"),
    chunks_per_day: int = Form(2),
    conn=Depends(get_db),
):
    book = conn.execute("SELECT id, title FROM books WHERE id = ?", (book_id,)).fetchone()
    if not book:
        return templates.TemplateResponse("index.html", {
            "request": request, "books": book_choices(conn),
            "message": "That book is not in the library.",
        })

    chunks_per_day = max(1, min(chunks_per_day, 12))
    manager = UserManager(conn)

    try:
        with db.transaction(conn):
            user_id, _ = manager.get_or_create_user(email, timezone_name=timezone_name)

            existing = conn.execute(
                """SELECT status FROM subscriptions
                   WHERE user_id = ? AND book_id = ?
                     AND status IN ('active', 'queued', 'paused', 'completed')""",
                (user_id, book_id),
            ).fetchone()

            if existing:
                already = {
                    "active": f"You are already reading '{book['title']}'.",
                    "paused": f"'{book['title']}' is paused - resume it from your dashboard.",
                    "queued": f"'{book['title']}' is already in your queue.",
                    "completed": f"You have already finished '{book['title']}'.",
                }[existing["status"]]
                raise _AlreadySubscribed(already)

            # First book starts now; anything after it waits its turn.
            active_count = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()[0]
            status = "active" if active_count == 0 else "queued"

            conn.execute(
                """INSERT INTO subscriptions
                       (user_id, book_id, status, current_sequence, chunks_per_day,
                        delivery_hour, last_sent, created_at)
                   VALUES (?, ?, ?, 1, ?, ?, NULL, ?)""",
                (user_id, book_id, status, chunks_per_day, dispatch.TARGET_HOUR, utc_now()),
            )
    except _AlreadySubscribed as already:
        return templates.TemplateResponse("index.html", {
            "request": request, "books": book_choices(conn), "message": str(already),
        })
    except Exception as exc:
        log(f"[ERROR] Signup failed: {type(exc).__name__}: {exc}")
        return templates.TemplateResponse("index.html", {
            "request": request, "books": book_choices(conn),
            "message": "Something went wrong creating your subscription.",
        })

    token = security.generate_dashboard_token(user_id)
    send_welcome_email(email, book["title"], f"{BASE_URL}/profile?token={token}",
                       is_queue=(status == "queued"))

    message = (f"Added '{book['title']}' to your queue. Check your email."
               if status == "queued" else
               f"Success! '{book['title']}' starts tomorrow. Check your email for your dashboard link.")

    return templates.TemplateResponse("index.html", {
        "request": request, "books": book_choices(conn), "message": message,
    })


class _AlreadySubscribed(Exception):
    """Signals a duplicate subscription so the transaction unwinds cleanly."""


# --- GETTING BACK IN (replaces username/password login) ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def handle_login(request: Request, email: str = Form(...), conn=Depends(get_db)):
    user = UserManager(conn).get_user_by_email(email)

    # Always say the same thing: whether an address is registered is not
    # something a stranger should be able to probe.
    if user:
        send_dashboard_link(email, f"{BASE_URL}/profile?token="
                                   f"{security.generate_dashboard_token(user['id'])}")

    return templates.TemplateResponse("login.html", {
        "request": request,
        "message": "If that address has a subscription, its dashboard link is on its way.",
    })


# --- DASHBOARD ---

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, token: str, conn=Depends(get_db)):
    user = user_from_token(conn, token)
    if not user:
        return invalid_link()

    current = conn.execute(
        """SELECT * FROM subscriptions WHERE user_id = ?
             AND status IN ('active', 'paused')
           ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at LIMIT 1""",
        (user["id"],),
    ).fetchone()

    queued = conn.execute(
        """SELECT s.id AS subscription_id, b.id AS book_id, b.title, b.author
           FROM subscriptions s JOIN books b ON b.id = s.book_id
           WHERE s.user_id = ? AND s.status = 'queued' ORDER BY s.created_at""",
        (user["id"],),
    ).fetchall()

    context = {
        "request": request,
        "token": token,
        "user_tz": user["timezone"],
        "is_admin": user["role"] == "admin",
        "queued_books": [dict(row) for row in queued],
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

    conn.execute("UPDATE subscriptions SET status = ? WHERE id = ?",
                 ("paused" if sub["status"] == "active" else "active", sub_id))
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
        conn.execute(
            "UPDATE subscriptions SET delivery_hour = ?, chunks_per_day = ? WHERE id = ?",
            (max(0, min(delivery_hour, 23)), max(1, min(chunks_per_day, 12)), sub_id),
        )

    return RedirectResponse(url=f"/profile?token={token}", status_code=303)


@app.get("/switch_book", response_class=HTMLResponse)
async def switch_book(token: str, new_book_id: str, conn=Depends(get_db)):
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
        conn.execute(
            """UPDATE subscriptions SET status = 'queued'
               WHERE user_id = ? AND status IN ('active', 'paused')""",
            (user["id"],),
        )

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
        else:
            conn.execute(
                """INSERT INTO subscriptions
                       (user_id, book_id, status, current_sequence, chunks_per_day,
                        delivery_hour, last_sent, created_at)
                   VALUES (?, ?, 'active', 1, 2, ?, NULL, ?)""",
                (user["id"], new_book_id, dispatch.TARGET_HOUR, utc_now()),
            )

    return simple_page(
        "You're switched!",
        f"<p style=\"font-size: 18px;\">You are now reading <strong>{book['title']}</strong>.</p>"
        f"<p>Your next part arrives tomorrow morning.</p>",
        colour="#28a745",
    )


# --- EMAIL LINKS (scoped to one subscription) ---

@app.get("/next", response_class=HTMLResponse)
async def trigger_next_chapter(token: str, conn=Depends(get_db)):
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


@app.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_process(token: str = Form(...), conn=Depends(get_db)):
    sub_id = verify_unsub_token(token)
    if not sub_id:
        return invalid_link()

    cur = conn.execute(
        "UPDATE subscriptions SET status = 'unsubscribed' WHERE id = ? AND status != 'unsubscribed'",
        (int(sub_id),),
    )
    return simple_page(
        "Unsubscribe",
        "<p>You have been unsubscribed.</p>" if cur.rowcount
        else "<p>That subscription was already closed.</p>",
    )


# --- SUGGESTIONS ---

@app.get("/suggest", response_class=HTMLResponse)
async def suggest_page(request: Request):
    return templates.TemplateResponse("suggest.html", {"request": request})


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

    return templates.TemplateResponse("admin.html", {"request": request, "users": user_data})
