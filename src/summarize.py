import time
import sys
import datetime
from pymongo import MongoClient
import google.generativeai as genai
import config

# --- SETUP ---
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
    except:
        model = genai.GenerativeModel('gemini-2.0-flash-lite')
else:
    print("Error: GEMINI_API_KEY not found.")
    sys.exit(1)

def log(msg):
    """Helper to print with timestamps for the log file"""
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_ai_summary(text):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            prompt = f"Summarize the following narrative text in exactly one sentence, written in the style of a 'Previously on...' TV recap. Keep it under 50 words:\n\n{text}"
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            if "429" in str(e):
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 30
                    log(f"   [AI Warning]: Rate limit hit (429). Cooling down for {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    log(f"   [AI STOP]: Daily rate limit exhausted. Stopping for today.")
                    # Exit with 0 (Success) so Cron doesn't think the script 'crashed'
                    sys.exit(0)
            
            # 404 Fallback logic
            if "404" in str(e) and "flash" in model.model_name:
                 try:
                     fallback = genai.GenerativeModel('gemini-2.0-flash-lite')
                     return fallback.generate_content(prompt).text.strip()
                 except:
                     return None
            
            log(f"   [AI Error]: {e}")
            return None
    return None

def process_book(book_id):
    # Get all chunks sorted by sequence
    chunks = list(db.chunks.find({"book_id": book_id}).sort("sequence", 1))
    
    if not chunks:
        return

    updates = 0
    previous_chunk_content = None
    
    # Check if we need to do work before logging to keep logs clean
    needs_work = any(c.get('recap') is None for c in chunks)
    if not needs_work:
        return

    log(f"Processing Book: {book_id}...")

    for chunk in chunks:
        seq = chunk['sequence']
        current_recap = chunk.get('recap')
        content = chunk['content']

        if current_recap is None:
            if seq == 1:
                new_recap = "You are beginning the book."
            else:
                if previous_chunk_content:
                    log(f"   Generating summary for Chunk {seq}...")
                    new_recap = get_ai_summary(previous_chunk_content)
                    # 8s delay = ~7 chunks/min. Safe for free tier.
                    time.sleep(8) 
                else:
                    new_recap = "Previously..."

            if new_recap:
                db.chunks.update_one(
                    {"_id": chunk['_id']},
                    {"$set": {"recap": new_recap}}
                )
                updates += 1
        
        previous_chunk_content = content

    if updates > 0:
        log(f"   -> Completed {updates} chunks for {book_id}.")

if __name__ == "__main__":
    log("--- Starting AI Backfill Worker ---")
    
    # Find all books
    books = list(db.books.find({}, {"book_id": 1}))
    
    for book in books:
        process_book(book['book_id'])
    
    log("--- Run Complete (All books checked) ---")
