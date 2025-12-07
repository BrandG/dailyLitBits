from pymongo import MongoClient
from user_manager import UserManager
import config

# Connect
db = MongoClient(config.MONGO_URI)[config.DB_NAME]
manager = UserManager(db)

# 1. Grab a random user (The "Ghost")
user = db.users.find_one({"is_claimed": {"$ne": True}})

if not user:
    print("No unclaimed users found. Create one on the homepage first!")
    exit()

print(f"Testing Auth for User ID: {user['_id']}")

# 2. Try to Claim Account
username = "TestCaptain"
password = "SecretPassword123"

success, msg = manager.claim_account(user['_id'], username, password)
print(f"Claim Attempt: {msg}")

if success:
    # 3. Try to Verify (Login)
    print("Testing Login...")
    logged_in_id = manager.verify_user(username, password)
    
    if logged_in_id == user['_id']:
        print("SUCCESS: Login verified!")
    else:
        print("FAILURE: Login failed.")
        
    # Cleanup (Reset user so you can test again later if needed)
    # db.users.update_one({"_id": user['_id']}, {"$set": {"is_claimed": False, "username": None}})
