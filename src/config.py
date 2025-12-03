import os
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# Database
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DB_NAME", "slow_reader_db")

# Security
key = os.getenv("ENCRYPTION_KEY")
if not key:
    raise ValueError("No ENCRYPTION_KEY set in .env file")
ENCRYPTION_KEY = key.encode() # Convert string back to bytes

# Email
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
