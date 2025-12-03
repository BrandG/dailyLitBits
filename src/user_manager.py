import argparse
import hashlib
from pymongo import MongoClient
from cryptography.fernet import Fernet
import config 

class UserManager:
    def __init__(self, db):
        self.users = db['users']
        self.cipher = Fernet(config.ENCRYPTION_KEY) # <--- Use config

    def _hash_email(self, email):
        clean_email = email.lower().strip().encode()
        return hashlib.sha256(clean_email).hexdigest()

    def _encrypt_email(self, email):
        return self.cipher.encrypt(email.encode())

    def create_user(self, email, timezone="UTC"):
        user_id = self._hash_email(email)
        
        if self.users.find_one({"_id": user_id}):
            print(f"User already exists: {user_id}")
            return user_id

        user_doc = {
            "_id": user_id,
            "email_enc": self._encrypt_email(email),
            "timezone": timezone,
            "created_at": "2023-10-27"
        }
        
        self.users.insert_one(user_doc)
        print(f"User created with ID: {user_id}")
        return user_id

if __name__ == "__main__":
    # CLI Argument Parsing
    parser = argparse.ArgumentParser(description="Manage DailyLitBits Users")
    parser.add_argument("email", help="The user's email address")
    parser.add_argument("--timezone", default="UTC", help="User's timezone")
    
    args = parser.parse_args()
    
    # Setup DB
    client = MongoClient(config.MONGO_URI)
    db = client[config.DB_NAME]
    
    manager = UserManager(db)
    manager.create_user(args.email, args.timezone)
