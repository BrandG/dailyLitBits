"""Users. One kind of user, one way in.

A reader is an email address. There is no username, no password and no
"claimed" state: identity is proved by a signed link mailed to that address
(see security.py). Whether someone is an admin is the `role` column on the
same row - a permission, not a second mechanism.

Addresses are Fernet-encrypted at rest. Fernet output is non-deterministic, so
the ciphertext cannot be searched; `email_lookup` holds an HMAC of the
normalised address instead, which gives an indexed unique lookup without
keeping the address in the clear. The old version scanned and decrypted every
row for each signup, and printed the plaintext while doing it - nothing here
logs an address.
"""
import hashlib
import hmac
from datetime import datetime, timezone

from cryptography.fernet import Fernet

import config


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise(email):
    return email.strip().lower()


class UserManager:
    def __init__(self, conn):
        self.conn = conn
        self.cipher = Fernet(config.ENCRYPTION_KEY)

    # --- addresses ---

    def encrypt_email(self, email):
        return self.cipher.encrypt(normalise(email).encode())

    def decrypt_email(self, email_enc):
        return self.cipher.decrypt(email_enc).decode()

    def email_lookup(self, email):
        """The searchable half of the pair: a keyed hash, not a reversible one."""
        return hmac.new(
            config.SECRET_KEY.encode(), normalise(email).encode(), hashlib.sha256
        ).hexdigest()

    # --- lookups ---

    def get_user_by_email(self, email):
        return self.conn.execute(
            "SELECT * FROM users WHERE email_lookup = ?", (self.email_lookup(email),)
        ).fetchone()

    def get_user(self, user_id):
        return self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()

    def email_for(self, user_id):
        """Decrypted address, or None if the row or the key is wrong."""
        row = self.get_user(user_id)
        if not row:
            return None
        try:
            return self.decrypt_email(row["email_enc"])
        except Exception:
            return None

    # --- creation ---

    def get_or_create_user(self, email, timezone_name="UTC"):
        """Returns (user_id, created). Signing up twice is not an error here."""
        existing = self.get_user_by_email(email)
        if existing:
            return existing["id"], False

        cur = self.conn.execute(
            """INSERT INTO users (email_enc, email_lookup, timezone, role, created_at)
               VALUES (?, ?, ?, 'reader', ?)""",
            (
                self.encrypt_email(email),
                self.email_lookup(email),
                timezone_name,
                utc_now(),
            ),
        )
        return cur.lastrowid, True

    # --- settings and permissions ---

    def set_timezone(self, user_id, timezone_name):
        self.conn.execute(
            "UPDATE users SET timezone = ? WHERE id = ?", (timezone_name, user_id)
        )

    def is_admin(self, user_id):
        row = self.get_user(user_id)
        return bool(row) and row["role"] == "admin"

    def set_role(self, user_id, role):
        if role not in ("reader", "admin"):
            raise ValueError("role must be 'reader' or 'admin'")
        cur = self.conn.execute(
            "UPDATE users SET role = ? WHERE id = ?", (role, user_id)
        )
        return cur.rowcount > 0
