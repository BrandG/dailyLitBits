from logger import log
import secrets
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from cryptography.fernet import Fernet
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pymongo import MongoClient
from bson import ObjectId
import config
from user_manager import UserManager
from subscribe import subscribe_user 
from security import verify_unsub_token, verify_binge_token
import dispatch 
from datetime import datetime
import security # Ensure this is imported
from fastapi.staticfiles import StaticFiles
import pymongo # Added for DuplicateKeyError
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# Add this AFTER creating the app = FastAPI() line
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

security_auth = HTTPBasic()
cipher = Fernet(config.ENCRYPTION_KEY)

# --- ADMIN CREDENTIALS ---
# In a real production app, put these in your .env file!
ADMIN_USER = "admin"
ADMIN_PASS = "change_this_password"

def get_current_admin(credentials: HTTPBasicCredentials = Depends(security_auth)):
    """Checks basic auth username/password"""
    is_user_ok = secrets.compare_digest(credentials.username, ADMIN_USER)
    is_pass_ok = secrets.compare_digest(credentials.password, ADMIN_PASS)

    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

async def get_db():
    client = MongoClient(config.MONGO_URI)
    try:
        yield client[config.DB_NAME]
    finally:
        client.close()

def send_welcome_email(to_email, book_title, dashboard_link, is_queue=False):
    """Sends a welcome email with the magic link."""
    if is_queue:
        subject = f"Added to Queue: {book_title}"
        body = f"""
        <html>
        <body style="font-family: sans-serif; padding: 20px;">
            <h2 style="color: #2c3e50;">You're in the queue!</h2>
            <p><strong>{book_title}</strong> has been added to your reading list.</p>
            <p>It will start automatically once you finish your current book.</p>
            <p><a href="{dashboard_link}" style="background-color: #28a745; color: white; padding: 10px 15px; text-decoration: none; border-radius: 5px;">Go to Dashboard</a></p>
        </body>
        </html>
        """
    else:
        subject = f"Welcome to dailyLitBits: {book_title}"
        body = f"""
        <html>
        <body style="font-family: sans-serif; padding: 20px;">
            <h2 style="color: #2c3e50;">Welcome to dailyLitBits!</h2>
            <p>You have successfully subscribed to <strong>{book_title}</strong>.</p>
            <p>Your first part will arrive tomorrow morning at 6:00 AM.</p>
            <p>You can manage your subscription, view your progress, or read ahead using your personal dashboard:</p>
            <p><a href="{dashboard_link}" style="background-color: #28a745; color: white; padding: 10px 15px; text-decoration: none; border-radius: 5px;">Access My Dashboard</a></p>
            <br>
            <p style="color: #777; font-size: 12px;">(You can claim your account on the dashboard to save your history permanently.)</p>
        </body>
        </html>
        """
    
    message = Mail(
        from_email=config.FROM_EMAIL,
        to_emails=to_email,
        subject=subject,
        html_content=body
    )
    try:
        sg = SendGridAPIClient(config.SENDGRID_API_KEY)
        sg.send(message)
        log(f"[INFO] Welcome email sent to {to_email}")
    except Exception as e:
        log(f"[ERROR] Failed to send welcome email: {e}")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db: MongoClient = Depends(get_db)):
    # FILTER: Only show Standard Edition (750 words) to keep the list clean
    books = list(db.books.find(
        {"chunk_size": 750}, 
        {"book_id": 1, "title": 1, "author": 1, "_id": 0}
    ).sort("title", 1)) # Added sort for niceness
    
    return templates.TemplateResponse("index.html", {
        "request": request, "books": books
    })

@app.post("/signup", response_class=HTMLResponse)
async def handle_signup(
    request: Request,
    email: str = Form(...),
    book_id: str = Form(...),
    timezone: str = Form("UTC"),
    db: MongoClient = Depends(get_db)
):
    manager = UserManager(db)

    # 1. Create User (or get existing ID)
    try:
        user_id = manager.create_user(email, timezone=timezone)
    except ValueError:
        # Email exists. Fetch the existing user.
        existing_user = manager.get_user_by_email(email)
        if not existing_user:
             return HTMLResponse("<h1>Error: Email conflict but user not found.</h1>", status_code=500)
        user_id = existing_user['_id']
        log(f"[INFO] Existing user found: {user_id}. Proceeding with subscription.")
    except pymongo.errors.DuplicateKeyError as e: 
        log(f"[ERROR] Signup Failed: {type(e).__name__}: {e}")
        return HTMLResponse("<h1>Error: Could not create user. Email might be taken.</h1>", status_code=500)

    # 2. Check for Duplicate Subscription to THIS book
    existing_sub = db.subscriptions.find_one({
        "user_id": user_id, 
        "book_id": book_id,
        "status": {"$in": ["active", "queued", "completed"]}
    })
    
    if existing_sub:
        books = list(db.books.find({}, {"book_id": 1, "title": 1, "author": 1, "_id": 0}))
        book_info = db.books.find_one({"book_id": book_id})
        book_title = book_info['title'] if book_info else book_id
        
        status_msg = existing_sub['status']
        if status_msg == "active":
            msg = f"You are already currently reading '{book_title}'."
        elif status_msg == "queued":
            msg = f"'{book_title}' is already in your queue."
        else:
            msg = f"You have already finished '{book_title}'."

        return templates.TemplateResponse("index.html", {
            "request": request,
            "books": books,
            "message": msg
        })

    # 3. Create Subscription
    # Check if user already has an active subscription
    active_subs_count = db.subscriptions.count_documents({"user_id": user_id, "status": "active"})

    new_status = "active" if active_subs_count == 0 else "queued"
    
    sub_data = {
        "user_id": user_id,
        "book_id": book_id,
        "current_sequence": 1,
        "status": new_status,
        "created_at": datetime.now(),
        "last_sent": None
    }

    try:
        result = db.subscriptions.insert_one(sub_data)
        sub_id = result.inserted_id
    except Exception as e:
        log(f"[ERROR] Subscription Failed: {e}")
        return HTMLResponse("<h1>Error: Could not create subscription.</h1>", status_code=500)

    # 4. Success Response & Email
    books = list(db.books.find({}, {"book_id": 1, "title": 1, "author": 1, "_id": 0}))
    book_info = db.books.find_one({"book_id": book_id})
    book_title = book_info['title'] if book_info else book_id
    
    # Generate Token for Magic Link
    token = security.generate_binge_token(sub_id)
    # NOTE: In production, use the actual domain from config or request.base_url
    dashboard_link = f"https://dailylitbits.com/profile?token={token}" 
    
    # Send Email
    send_welcome_email(email, book_title, dashboard_link, is_queue=(new_status == "queued"))

    if new_status == "queued":
        message = f"Added '{book_title}' to your reading queue. Check your email for the confirmation."
    else: # new_status == "active"
        message = f"Success! You will start receiving '{book_title}' tomorrow. Check your email for your dashboard link."

    return templates.TemplateResponse("index.html", {
        "request": request,
        "books": books,
        "message": message
    })

@app.get("/next", response_class=HTMLResponse)
async def trigger_next_chapter(request: Request, token: str, db: MongoClient = Depends(get_db)):
    sub_id_str = verify_binge_token(token)
    
    if not sub_id_str:
        return HTMLResponse(content="<h1>Invalid or Expired Link</h1>", status_code=400)
    
    success, msg = dispatch.process_subscription(sub_id_str, trigger='binge')
    
    color = "#28a745" if success else "#dc3545"
    
    html_content = f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px; color: #333;">
        <div style="max-width: 500px; margin: 0 auto;">
            <h1 style="color: {color};">{'On its way!' if success else 'Hold on...'}</h1>
            <p style="font-size: 18px;">{msg}</p>
            <p style="margin-top: 30px;"><a href="/" style="color: #666;">Return to DailyLitBits</a></p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, admin: str = Depends(get_current_admin), db: MongoClient = Depends(get_db)):

    # 1. Fetch all subscriptions
    subs = list(db.subscriptions.find({}))

    user_data = []

    for sub in subs:
        # Get User Email (Decrypt it)
        user = db.users.find_one({"_id": sub['user_id']})
        email_display = "Unknown"
        timezone = "UTC"
        if user:
            try:
                email_display = cipher.decrypt(user['email_enc']).decode()
                timezone = user.get('timezone', 'UTC')
            except:
                email_display = "[Decryption Failed]"

        # Get Book Info
        book = db.books.find_one({"book_id": sub['book_id']})
        if book:
            book_title = book.get('title', sub['book_id'])
            total_chunks = book.get('total_chunks', 1)
        else:
            book_title = sub['book_id']
            total_chunks = 1

        if total_chunks == 0: total_chunks = 1 # avoid div by zero

        # Calculate Progress
        current = sub.get('current_sequence', 1)
        percent = int((current / total_chunks) * 100)

        # Format Last Sent
        last_sent_raw = sub.get('last_sent')
        last_sent = last_sent_raw.strftime("%Y-%m-%d %H:%M") if last_sent_raw else "Never"

        user_data.append({
            "email": email_display,
            "timezone": timezone,
            "book_title": book_title,
            "current": current,
            "total": total_chunks,
            "percent": percent,
            "status": sub.get("status", "unknown"),
            "last_sent": last_sent
        })

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "users": user_data
    })

# --- NEW: STEP 1 - SHOW CONFIRMATION PAGE ---
@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_confirm(request: Request, token: str, db: MongoClient = Depends(get_db)):
    # Just verify token validity, don't unsubscribe yet
    sub_id_str = verify_unsub_token(token)
    
    if not sub_id_str:
        return HTMLResponse(content="<h1>Invalid or Expired Link</h1>", status_code=400)

    # Show a confirmation button
    html_content = f"""
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
    """
    return HTMLResponse(content=html_content)

# --- NEW: STEP 2 - ACTUALLY UNSUBSCRIBE ---
@app.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_process(request: Request, token: str = Form(...), db: MongoClient = Depends(get_db)):
    sub_id_str = verify_unsub_token(token)
    
    if not sub_id_str:
        return HTMLResponse(content="<h1>Invalid or Expired Link</h1>", status_code=400)

    result = db.subscriptions.update_one(
        {"_id": ObjectId(sub_id_str)}, 
        {"$set": {"status": "unsubscribed"}}
    )

    msg = "You have been successfully unsubscribed." if result.modified_count > 0 else "Subscription not found or already unsubscribed."

    html_content = f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
        <h1>Unsubscribe</h1>
        <p>{msg}</p>
        <p><a href="/">Return to DailyLitBits</a></p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# --- NEW: ONE-CLICK BOOK SWITCH ---
@app.get("/switch_book", response_class=HTMLResponse)
async def switch_book(request: Request, token: str, new_book_id: str, db: MongoClient = Depends(get_db)):

    # 1. Verify User
    sub_id_str = verify_binge_token(token)
    if not sub_id_str:
        return HTMLResponse(content="<h1>Invalid or Expired Link</h1>", status_code=400)

    # 2. Get Book Info (for the success message)
    new_book = db.books.find_one({"book_id": new_book_id})
    if not new_book:
        return HTMLResponse(content="<h1>Book not found</h1>", status_code=404)

    # 3. Reset Subscription
    # We keep the same subscription ID but change the book and reset the counter
    db.subscriptions.update_one(
        {"_id": ObjectId(sub_id_str)},
        {
            "$set": {
                "book_id": new_book_id,
                "current_sequence": 1,
                "status": "active",
                "created_at": datetime.now(), # Reset start time for stats
                "last_sent": None # Allows them to receive Part 1 immediately if they want
            }
        }
    )

    # 4. Success Page
    html_content = f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px; color: #333;">
        <div style="max-width: 500px; margin: 0 auto;">
            <h1 style="color: #28a745;">You're Switched!</h1>
            <p style="font-size: 18px;">You are now reading <strong>{new_book['title']}</strong>.</p>
            <p>Your first part will arrive tomorrow morning.</p>
            <p style="font-size: 14px; color: #666; margin-top: 30px;">(Or check your email—we might slip the first part in sooner!)</p>
            <p style="margin-top: 30px;"><a href="/" style="color: #666;">Return to DailyLitBits</a></p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/claim_profile", response_class=HTMLResponse)
async def process_claim(
    request: Request,
    token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    db: MongoClient = Depends(get_db)
):

    # 1. Verify Token again
    sub_id_str = verify_binge_token(token)
    if not sub_id_str:
        return HTMLResponse(content="<h1>Invalid or Expired Link</h1>", status_code=400)

    sub = db.subscriptions.find_one({"_id": ObjectId(sub_id_str)})
    if not sub: return HTMLResponse("Sub not found")

    # 2. Attempt Claim
    manager = UserManager(db)
    print(f"[DEBUG] Attempting claim for User {sub['user_id']} with username '{username}'")

    success, msg = manager.claim_account(sub['user_id'], username, password)
    print(f"[DEBUG] Claim Result: Success={success}, Msg='{msg}'") 


    if not success:
        # Show form again with error
        return templates.TemplateResponse("claim_profile.html", {
            "request": request,
            "token": token,
            "error": msg # e.g. "Username taken"
        })

    # 3. Success!
    return RedirectResponse(url=f"/profile?token={token}", status_code=303)


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, token: str, db: MongoClient = Depends(get_db)):

    # 1. Verify Token
    sub_id_str = verify_binge_token(token)
    if not sub_id_str:
        return HTMLResponse(content="<h1>Invalid or Expired Link</h1>", status_code=400)

    # 2. Get Data
    sub = db.subscriptions.find_one({"_id": ObjectId(sub_id_str)})
    if not sub: return HTMLResponse("Sub not found", status_code=404)

    user = db.users.find_one({"_id": sub['user_id']})

    # If UNCLAIMED, show the claim form (Goal 1 logic)
    if not user.get('is_claimed'):
        return templates.TemplateResponse("claim_profile.html", {
            "request": request,
            "token": token
        })

    # If CLAIMED, show the Dashboard (Goal 2 logic)
    # Fetch Book Info
    book = db.books.find_one({"book_id": sub['book_id']})

    # Calc Stats
    current = sub.get('current_sequence', 1)
    total = book.get('total_chunks', 100) if book else 100
    percent = int((current / total) * 100)

    start_date = sub.get('created_at')
    start_str = start_date.strftime('%b %d, %Y') if start_date else "Unknown"

    # DETERMINE CURRENT EDITION
    book_id = sub['book_id']
    current_edition = "standard"
    edition_label = "Standard (~750 words)"
    
    if book_id.endswith("_short"):
        current_edition = "short"
        edition_label = "Short (~325 words)"
    elif book_id.endswith("_long"):
        current_edition = "long"
        edition_label = "Long (~1500 words)"

    # Fetch queued books for the 'Up Next' section
    queued_subscriptions = list(db.subscriptions.find(
        {"user_id": sub['user_id'], "status": "queued"},
        sort=[("created_at", 1)] # Oldest first
    ))

    queued_books_data = []
    for q_sub in queued_subscriptions:
        q_book = db.books.find_one({"book_id": q_sub['book_id']})
        if q_book:
            queued_books_data.append({
                "title": q_book.get('title', q_sub['book_id']),
                "author": q_book.get('author', 'Unknown Author'),
                "book_id": q_book['book_id'],
                "subscription_id": str(q_sub['_id'])
            })

    # Add user timezone and delivery hour to context if missing
    user_tz = user.get('timezone', 'UTC')
    delivery_hour = sub.get('delivery_hour', 6) # Default to 6 AM as in dispatch.py

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "token": token,
        "username": user.get('username'),
        "user_tz": user_tz, # Pass it to template
        "delivery_hour": delivery_hour, # Pass it to template
        "book_title": book.get('title', 'Unknown'),
        "book_author": book.get('author', 'Unknown'),
        "current": current,
        "total": total,
        "percent": percent,
        "start_date": start_str,
        "status": sub.get('status', 'active'),
        # NEW FIELDS
        "current_edition": current_edition,
        "edition_label": edition_label,
        # ADDING THIS
        "queued_books": queued_books_data
    })

# --- NEW: VACATION MODE TOGGLE ---
@app.post("/toggle_pause", response_class=HTMLResponse)
async def toggle_pause(request: Request, token: str = Form(...), db: MongoClient = Depends(get_db)):
    sub_id_str = verify_binge_token(token)
    if not sub_id_str: return HTMLResponse("Invalid Token", status_code=400)

    sub = db.subscriptions.find_one({"_id": ObjectId(sub_id_str)})

    # Toggle Logic
    new_status = 'paused' if sub['status'] == 'active' else 'active'

    db.subscriptions.update_one(
        {"_id": sub['_id']},
        {"$set": {"status": new_status}}
    )

    # Redirect back to profile to see change
    # We use a simple meta refresh or JS redirect to keep the token in URL
    # Or just re-render the profile function (cleaner is redirect, but needs token in URL)

    # For simplicity, we redirect back to the GET route with the token
    return RedirectResponse(url=f"/profile?token={token}", status_code=303)

@app.post("/update_preferences", response_class=HTMLResponse)
async def update_preferences(
    request: Request, 
    token: str = Form(...), 
    timezone: str = Form(...), 
    delivery_hour: int = Form(...), 
    db: MongoClient = Depends(get_db)
):
    sub_id_str = verify_binge_token(token)
    if not sub_id_str: return HTMLResponse("Invalid Token", status_code=400)
    
    sub = db.subscriptions.find_one({"_id": ObjectId(sub_id_str)})
    if not sub: return HTMLResponse("Subscription not found", status_code=404)
    
    # 1. Update User Timezone
    db.users.update_one(
        {"_id": sub['user_id']},
        {"$set": {"timezone": timezone}}
    )
    
    # 2. Update Subscription Delivery Hour
    db.subscriptions.update_one(
        {"_id": sub['_id']},
        {"$set": {"delivery_hour": delivery_hour}}
    )
    
    return RedirectResponse(url=f"/profile?token={token}", status_code=303)

# --- SUGGESTION ROUTES ---

@app.get("/suggest", response_class=HTMLResponse)
async def suggest_page(request: Request):
    return templates.TemplateResponse("suggest.html", {"request": request})

@app.post("/suggest", response_class=HTMLResponse)
async def handle_suggestion(
    request: Request,
    title: str = Form(...),
    author: str = Form(...),
    link: str = Form(None),
    comments: str = Form(None),
    db: MongoClient = Depends(get_db)
):

    # Save to new 'suggestions' collection
    suggestion = {
        "title": title,
        "author": author,
        "link": link,
        "comments": comments,
        "submitted_at": datetime.now(),
        "status": "pending" # pending, approved, rejected
    }

    db.suggestions.insert_one(suggestion)

    # Success Page
    html_content = f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
        <h1 style="color: green;">Thank You!</h1>
        <p>We have received your suggestion for <strong>{title}</strong>.</p>
        <p>If we add it to the library, it will appear on the homepage soon.</p>
        <br>
        <a href="/">Return Home</a>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/change_edition", response_class=HTMLResponse)
async def change_edition(request: Request, token: str = Form(...), target_edition: str = Form(...), db: MongoClient = Depends(get_db)):
    sub_id_str = verify_binge_token(token)
    if not sub_id_str: return HTMLResponse("Invalid Token", status_code=400)
    
    sub = db.subscriptions.find_one({"_id": ObjectId(sub_id_str)})
    current_book_id = sub['book_id']
    
    # 1. Determine Base ID
    # Remove existing suffixes to get the "clean" ID (e.g. pg16_short -> pg16)
    base_id = current_book_id.replace("_short", "").replace("_long", "")
    
    # 2. Construct New ID
    new_book_id = base_id
    if target_edition == "short":
        new_book_id = f"{base_id}_short"
    elif target_edition == "long":
        new_book_id = f"{base_id}_long"
        
    # 3. Validation: Does the new book exist?
    new_book = db.books.find_one({"book_id": new_book_id})
    if not new_book:
        return HTMLResponse("<h1>Error: That edition is not available for this book.</h1>", status_code=404)
        
    # 4. Update Subscription (RESET PROGRESS)
    db.subscriptions.update_one(
        {"_id": sub['_id']},
        {
            "$set": {
                "book_id": new_book_id,
                "current_sequence": 1, # <--- RESET
                "status": "active",
                "last_sent": None     # Allow immediate send
            }
        }
    )
    
    return RedirectResponse(url=f"/profile?token={token}", status_code=303)

# --- LOGIN ROUTES ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", response_class=HTMLResponse)
async def handle_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: MongoClient = Depends(get_db)
):
    manager = UserManager(db)
    
    # 1. Verify Credentials
    # Returns user_id if valid, None if invalid
    user_id = manager.verify_user(username, password)
    
    if not user_id:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid username or password."
        })
        
    # 2. Find Subscription (to generate the token)
    # We use the token as our "Session Cookie" for now
    sub = db.subscriptions.find_one({"user_id": user_id})
    
    if not sub:
        return HTMLResponse("<h1>Error: No active subscription found for this user.</h1>", status_code=404)
        
    # 3. Generate Magic Link
    token = security.generate_binge_token(sub['_id'])
    
    # 4. Redirect to Dashboard
    return RedirectResponse(url=f"/profile?token={token}", status_code=303)

@app.get("/library", response_class=HTMLResponse)
async def library(request: Request, db: MongoClient = Depends(get_db)):
    # 1. Fetch Books (Standard Edition)
    books_cursor = db.books.find(
        {"chunk_size": 750}, 
        {"book_id": 1, "title": 1, "author": 1, "total_chunks": 1, "description": 1, "_id": 0}
    ).sort("title", 1)
    
    books = []
    for b in books_cursor:
        # 2. Logic to generate Gutenberg Cover URL
        # Format: pg123 -> 123
        clean_id = b['book_id'].replace("pg", "")
        if clean_id.isdigit():
            b['cover_url'] = f"https://www.gutenberg.org/cache/epub/{clean_id}/pg{clean_id}.cover.medium.jpg"
        else:
            # Fallback for weird IDs or testing
            b['cover_url'] = "https://via.placeholder.com/150x230?text=No+Cover"
            
        books.append(b)
    
    return templates.TemplateResponse("library.html", {
        "request": request, 
        "books": books
    })

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})

@app.get("/intro", response_class=HTMLResponse)
async def intro_page(request: Request):
    return templates.TemplateResponse("intro.html", {"request": request})
