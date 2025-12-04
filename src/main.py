from fastapi import FastAPI, Request, Form
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

app = FastAPI()
templates = Jinja2Templates(directory="templates")

def get_db():
    client = MongoClient(config.MONGO_URI)
    return client[config.DB_NAME]

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    db = get_db()
    books = list(db.books.find({}, {"book_id": 1, "title": 1, "author": 1, "_id": 0}))
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
    manager.create_user(email, timezone=timezone)
    subscribe_user(email, book_id)
    
    books = list(db.books.find({}, {"book_id": 1, "title": 1, "_id": 0}))
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
