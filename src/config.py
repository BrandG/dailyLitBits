import os
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# Database
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DB_NAME", "slow_reader_db")

# SQLite. The default resolves correctly both on the host and inside the
# container: docker-compose mounts src/ at /app and ../data at /data, so
# dirname(dirname(config.py)) is the right parent in either place.
DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db"
)
DB_PATH = os.getenv("DB_PATH", DEFAULT_DB_PATH)

# Security
key = os.getenv("ENCRYPTION_KEY")
if not key:
    raise ValueError("No ENCRYPTION_KEY set in .env file")
ENCRYPTION_KEY = key.encode() # Convert string back to bytes

# Signing key for the magic links, and the HMAC key for users.email_lookup.
# Separate from ENCRYPTION_KEY on purpose: rotating the signing key should
# invalidate outstanding links without making stored addresses unreadable.
# Rotating THIS key does invalidate every outstanding link and every
# email_lookup value, so it is not a routine operation.
secret = os.getenv("SECRET_KEY")
if not secret:
    raise ValueError("No SECRET_KEY set in .env file")
SECRET_KEY = secret

# Email
SMTP_HOST = os.getenv("SMTP_SERVER", "smtp-relay.brevo.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 2525))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("EMAIL_SENDER")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini model id, in one place because Google retires these. gemini-2.0-flash
# was withdrawn and every AI feature here failed with a 404 until this moved.
# flash-lite is the cheap end, which matters when recap generation is one call
# per chunk; swap to gemini-3.5-flash for better prose. Check what the key can
# actually see with: docker exec dailylitbits python tools/model_list.py
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
