import pytz
from datetime import datetime
from cryptography.fernet import Fernet
from passlib.context import CryptContext # <--- NEW
import config

# Setup Password Hashing (Bcrypt)
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

class UserManager:
    def __init__(self, db):
        self.db = db
        self.cipher = Fernet(config.ENCRYPTION_KEY)

    def encrypt_email(self, email):
        return self.cipher.encrypt(email.lower().encode())

    def create_user(self, email, timezone="UTC"):
        """
        Creates a 'Ghost' user (no password, no username yet).
        """
        # Check if email exists (by searching all users - inefficient but fine for now)
        # Ideally, we'd hash emails for lookup, but for now we iterate or catch duplicates
        # optimizing this is a future task.
        
        user = {
            "email_enc": self.encrypt_email(email),
            "timezone": timezone,
            "created_at": datetime.now(),
            "username": None,      # <--- NEW
            "password_hash": None, # <--- NEW
            "is_claimed": False,   # <--- NEW
            "role": "reader"
        }
        
        result = self.db.users.insert_one(user)
        return result.inserted_id

    # --- NEW AUTHENTICATION METHODS ---

    def claim_account(self, user_id, username, password):
        """
        Upgrades a Ghost user to a Claimed user.
        """
        # 1. Hash the password
        hashed_pw = pwd_context.hash(password)

        # 2. Update DB
        try:
            # First, check if username is taken manually (clearer debugging)
            existing = self.db.users.find_one({"username": username})
            if existing and existing['_id'] != user_id:
                print(f"[ERROR] Username '{username}' is already taken by {existing['_id']}")
                return False, "Username already taken."

            result = self.db.users.update_one(
                {"_id": user_id},
                {
                    "$set": {
                        "username": username,
                        "password_hash": hashed_pw,
                        "is_claimed": True
                    }
                }
            )
            print(f"[DEBUG] Update Result: Matched={result.matched_count}, Modified={result.modified_count}")

            if result.matched_count == 0:
                return False, "User ID not found in database."

            return True, "Account claimed successfully."

        except Exception as e:
            # PRINT THE REAL ERROR
            print(f"[CRITICAL ERROR] Claim failed: {e}")
            return False, f"System Error: {e}"

    def verify_user(self, username, password):
        """
        Verifies login credentials. Returns user_id if valid, None if not.
        """
        user = self.db.users.find_one({"username": username})
        if not user:
            return None
        
        if not user.get('password_hash'):
            return None
            
        if pwd_context.verify(password, user['password_hash']):
            return user['_id']
        
        return None
