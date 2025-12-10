import sys
import os
# Add the parent directory (root) to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pymongo import MongoClient
from bson import ObjectId
import datetime
import config 

# Let config handle the connection string (it picks up the Docker env vars)
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

def setup_test():
    print("--- Setting up Victory Flow Test ---")
    
    # 1. Create a Fake Finished Book
    # We create a book with only 1 chunk so we can finish it instantly.
    book_id = "test_victory_book"
    db.books.update_one(
        {"book_id": book_id},
        {"$set": {
            "title": "The Shortest Story",
            "author": "Test Author",
            "total_chunks": 1,
            "edition": "standard"
        }},
        upsert=True
    )
    
    # Create the single chunk
    db.chunks.update_one(
        {"book_id": book_id, "sequence": 1},
        {"$set": {
            "content": "The end. It was a very short adventure.",
            "word_count": 8,
            "recap": None
        }},
        upsert=True
    )
    
    print(f"1. Created test book: {book_id}")

    # 2. Create a Test User
    # We need a real email to check if the SendGrid call works (or fails gracefully)
    test_email = "test@example.com" 
    
    # We can skip the encryption for this manual insert if we just want to test the dispatch logic,
    # BUT dispatch.py expects encrypted emails. 
    # Let's assume you have a user or can create one via your app. 
    # Actually, let's just grab the first user in the DB to use as our guinea pig.
    user = db.users.find_one()
    if not user:
        print("Error: No users found in DB. Run the app and sign up one user first.")
        return

    print(f"2. Using existing user ID: {user['_id']}")

    # 3. Create/Reset Subscription to be 'Ready to Finish'
    # We set current_sequence to 1. Since total_chunks is 1, 
    # the NEXT dispatch call should see they are done.
    
    # WAIT! Logic Check:
    # If current=1 and total=1...
    # dispatch.py pulls chunk #1. Sends it. Updates current to 2.
    # NEXT time it runs, it looks for chunk #2. Chunk #2 doesn't exist.
    # THEN it triggers Victory.
    
    # So we need to set the subscription to sequence=2 (which doesn't exist).
    
    db.subscriptions.update_one(
        {"user_id": user['_id'], "book_id": book_id},
        {"$set": {
            "current_sequence": 2, # This is > total_chunks (1)
            "status": "active",
            "created_at": datetime.datetime.now() - datetime.timedelta(days=5),
            "last_sent": datetime.datetime.now() - datetime.timedelta(days=1) # Ready to send
        }},
        upsert=True
    )
    
    print("3. Subscription configured. User is at Sequence 2 (Book has 1 chunk).")
    print("\nREADY TO TEST:")
    print("Run: python3 dispatch.py --debug --force")

if __name__ == "__main__":
    setup_test()
