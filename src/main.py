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

app = FastAPI()
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

def get_db():
    client = MongoClient(config.MONGO_URI)
    return client[config.DB_NAME]

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    db = get_db()
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
    timezone: str = Form("UTC")
):
    db = get_db()
    manager = UserManager(db)

    # 1. Create User (or get existing ID if we implement check later)
    # The create_user method returns the new _id
    try:
        user_id = manager.create_user(email, timezone=timezone)
    except Exception as e:
        # In case of duplicate key error or other DB issues
        print(f"[ERROR] Signup Failed: {e}")
        return HTMLResponse("<h1>Error: Could not create user. Email might be taken.</h1>", status_code=500)

    # 2. Create Subscription DIRECTLY (Bypassing subscribe.py)
    # This ensures we link exactly to the user we just created
    sub_data = {
        "user_id": user_id,
        "book_id": book_id,
        "current_sequence": 1,
        "status": "active",
        "created_at": datetime.now(),
        "last_sent": None
    }

    try:
        db.subscriptions.insert_one(sub_data)
    except Exception as e:
        print(f"[ERROR] Subscription Failed: {e}")
        return HTMLResponse("<h1>Error: Could not create subscription.</h1>", status_code=500)

    # 3. Success Response
    books = list(db.books.find({}, {"book_id": 1, "title": 1, "author": 1, "_id": 0}))
    book_info = db.books.find_one({"book_id": book_id})
    book_title = book_info['title'] if book_info else book_id

    return templates.TemplateResponse("index.html", {
        "request": request,
        "books": books,
        "message": f"Success! You will start receiving '{book_title}' tomorrow at 6:00 AM ({timezone})."
    })

@app.get("/next", response_class=HTMLResponse)
async def trigger_next_chapter(request: Request, token: str):
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
async def admin_dashboard(request: Request, admin: str = Depends(get_current_admin)):
    db = get_db()

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
        book_title = book['title'] if book else sub['book_id']
        total_chunks = book.get('total_chunks', 1)
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
async def unsubscribe_confirm(request: Request, token: str):
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
async def unsubscribe_process(request: Request, token: str = Form(...)):
    db = get_db()
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
async def switch_book(request: Request, token: str, new_book_id: str):
    db = get_db()

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
    password: str = Form(...)
):
    db = get_db()

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
async def profile_page(request: Request, token: str):
    db = get_db()

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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "token": token,
        "username": user.get('username'),
        "book_title": book.get('title', 'Unknown'),
        "book_author": book.get('author', 'Unknown'),
        "current": current,
        "total": total,
        "percent": percent,
        "start_date": start_str,
        "status": sub.get('status', 'active'),
        # NEW FIELDS
        "current_edition": current_edition,
        "edition_label": edition_label
    })

# --- NEW: VACATION MODE TOGGLE ---
@app.post("/toggle_pause", response_class=HTMLResponse)
async def toggle_pause(request: Request, token: str = Form(...)):
    db = get_db()

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
    comments: str = Form(None)
):
    db = get_db()

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
async def change_edition(request: Request, token: str = Form(...), target_edition: str = Form(...)):
    db = get_db()
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
    password: str = Form(...)
):
    db = get_db()
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

