from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pymongo import MongoClient
from bson import ObjectId
import config
from user_manager import UserManager
from subscribe import subscribe_user 
from security import verify_unsub_token

app = FastAPI()

templates = Jinja2Templates(directory="templates")

def get_db():
    client = MongoClient(config.MONGO_URI)
    return client[config.DB_NAME]

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    db = get_db()
    books = list(db.books.find({}, {"book_id": 1, "title": 1, "_id": 0}))
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "books": books
    })

@app.post("/signup", response_class=HTMLResponse)
async def handle_signup(
    request: Request, 
    email: str = Form(...), 
    book_id: str = Form(...)
):
    db = get_db()
    manager = UserManager(db)
    manager.create_user(email)
    subscribe_user(email, book_id)
    
    books = list(db.books.find({}, {"book_id": 1, "title": 1, "_id": 0}))
    book_info = db.books.find_one({"book_id": book_id})
    book_title = book_info['title'] if book_info else book_id

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "books": books,
        "message": f"Success! You will start receiving '{book_title}' tomorrow."
    })

@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(request: Request, token: str):
    db = get_db()
    
    # 1. Verify the Token
    sub_id_str = verify_unsub_token(token)
    
    if not sub_id_str:
        return HTMLResponse(content="<h1>Invalid or Expired Link</h1>", status_code=400)

    # 2. Update the Database
    # We set status to 'unsubscribed' so dispatch.py will ignore it
    result = db.subscriptions.update_one(
        {"_id": ObjectId(sub_id_str)}, 
        {"$set": {"status": "unsubscribed"}}
    )

    if result.modified_count == 0:
        # It might already be unsubscribed, or ID not found
        msg = "Subscription not found or already unsubscribed."
    else:
        msg = "You have been successfully unsubscribed."

    # 3. Show simple confirmation page
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
