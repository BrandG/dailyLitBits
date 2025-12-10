import requests
import os
import shutil
import time
from pymongo import MongoClient
import google.generativeai as genai
import sys

# 1. PATH SETUP (Robust)
# Get the directory of this script (src/tools)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
# Get the parent directory (src)
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)

# Add src to sys.path so we can import config
sys.path.append(PROJECT_ROOT)
import config

# --- CONFIGURATION ---
COVER_DIR = "static/covers"
# Use the model available in your list
GENAI_MODEL_NAME = 'models/gemini-flash-latest' 

# Connect to DB
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

# Configure AI
if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)

def download_cover(book_id):
    """
    Downloads the cover image from Project Gutenberg and saves it locally.
    Returns the local relative path (e.g., '/static/covers/11.jpg') or None.
    """
    # Convert 'pg11' or 'pg11_short' -> '11'
    clean_id = book_id.lower().replace("pg", "").replace("_short", "").replace("_long", "")
    
    if not clean_id.isdigit():
        return None
    
    # Target URL (Gutenberg standard format)
    remote_url = f"https://www.gutenberg.org/cache/epub/{clean_id}/pg{clean_id}.cover.medium.jpg"
    
    # Local Filename
    filename = f"{clean_id}.jpg"
    local_path = os.path.join(COVER_DIR, filename)
    public_path = f"/static/covers/{filename}"
    
    # 1. Check if we already have it
    if os.path.exists(local_path):
        return public_path
        
    # 2. Download it
    try:
        # User-Agent is polite and often required by Gutenberg
        headers = {'User-Agent': 'DailyLitBits-Indexer/1.0'}
        response = requests.get(remote_url, headers=headers, stream=True, timeout=10)
        
        if response.status_code == 200:
            with open(local_path, 'wb') as f:
                response.raw.decode_content = True
                shutil.copyfileobj(response.raw, f)
            print(f"   [Cover] Downloaded: {filename}")
            time.sleep(0.5) # Be nice to their server
            return public_path
        else:
            print(f"   [Cover] Not found for {book_id} (Status {response.status_code})")
            return None
            
    except Exception as e:
        print(f"   [Cover] Error downloading {book_id}: {e}")
        return None

def generate_blurb(title, author):
    """
    Asks Gemini to write a short hook for the book.
    """
    if not config.GEMINI_API_KEY:
        return None

    model = genai.GenerativeModel(GENAI_MODEL_NAME)
    
    prompt = f"""
    You are a bookstore curator. Write a short, enticing "hook" description (max 2 sentences) for the book "{title}" by {author}.
    
    RULES:
    1. Do NOT use phrases like "In this book", "This novel", or "Readers will".
    2. Jump straight into the premise, atmosphere, or conflict.
    3. Keep it under 40 words.
    
    EXAMPLE for Dracula:
    "A young solicitor travels to Transylvania to finalize a property deal, only to discover his client is an ancient vampire with sights set on London."
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"   [AI] Error generating blurb: {e}")
        return None

def backfill_library():
    # Ensure directory exists
    os.makedirs(COVER_DIR, exist_ok=True)
    
    # Find all "Standard" edition books
    # We use standard as the "master" record to generate metadata for all versions
    books = list(db.books.find({"edition": "std"}))
    
    print(f"--- Starting Library Enhancement (Found {len(books)} books) ---")
    
    for book in books:
        book_id = book['book_id']     # e.g., pg11
        base_id = book_id.replace("pg", "") # e.g., 11
        title = book['title']
        author = book['author']
        
        updates = {}
        
        # 1. HANDLE COVER
        # Only download if we don't have a URL saved yet
        if "cover_url" not in book:
            cover_path = download_cover(book_id)
            if cover_path:
                updates["cover_url"] = cover_path
        
        # 2. HANDLE DESCRIPTION
        # Only generate if missing
        if "description" not in book:
            print(f"   [AI] Generating blurb for '{title}'...")
            blurb = generate_blurb(title, author)
            if blurb:
                updates["description"] = blurb
                print(f"        -> {blurb}")
                time.sleep(1.0) # Rate limit protection
        
        # 3. SAVE UPDATES
        if updates:
            # We update ALL editions (short, std, long) that share this ID
            # Regex matches pg11, pg11_short, pg11_long
            regex_pattern = f"^pg{base_id}(_short|_long)?$"
            
            result = db.books.update_many(
                {"book_id": {"$regex": regex_pattern}},
                {"$set": updates}
            )
            print(f"   -> Updated {result.modified_count} editions for '{title}'")
        else:
            print(f"   -> Skipped '{title}' (Already complete)")

    print("--- Library Enhancement Complete ---")

if __name__ == "__main__":
    backfill_library()
