import argparse
import hashlib
from pymongo import MongoClient
from datetime import datetime
import config

def get_user_id(email):
    clean_email = email.lower().strip().encode()
    return hashlib.sha256(clean_email).hexdigest()

def subscribe_user(email, book_id):
    client = MongoClient(config.MONGO_URI)
    db = client[config.DB_NAME]
    
    user_id = get_user_id(email)
    
    # 1. Verify User
    if not db.users.find_one({"_id": user_id}):
        print(f"Error: User '{email}' not found. Create them first.")
        return

    # 2. Verify Book
    if not db.books.find_one({"book_id": book_id}):
        print(f"Error: Book '{book_id}' not found.")
        return

    # 3. Check Existing
    existing = db.subscriptions.find_one({"user_id": user_id, "book_id": book_id})
    if existing:
        print(f"Already subscribed. Status: {existing['status']}")
        return

    # 4. Subscribe
    sub_doc = {
        "user_id": user_id,
        "book_id": book_id,
        "current_sequence": 1,
        "status": "active",
        "start_date": datetime.utcnow(),
        "last_sent": None
    }
    
    db.subscriptions.insert_one(sub_doc)
    print(f"Success! '{email}' subscribed to '{book_id}'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Subscribe a user to a book")
    parser.add_argument("email", help="User email")
    parser.add_argument("book_id", help="Book ID (e.g., pg84)")
    
    args = parser.parse_args()
    
    subscribe_user(args.email, args.book_id)
