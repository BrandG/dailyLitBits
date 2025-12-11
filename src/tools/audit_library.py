from logger import log
import os
import sys
import json
import time
from pymongo import MongoClient
import google.generativeai as genai

# --- PATH SETUP ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
sys.path.append(PROJECT_ROOT)
import config

# --- CONFIG ---
# Use the stable model
GENAI_MODEL_NAME = 'models/gemini-flash-latest' 

client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)

def check_consistency(title, author, description):
    """
    Asks AI if the description matches the title.
    Returns: (is_match: bool, new_blurb: str|None)
    """
    model = genai.GenerativeModel(GENAI_MODEL_NAME)
    
    prompt = f"""
    You are a data integrity auditor for a library.
    
    BOOK DETAILS:
    Title: {title}
    Author: {author}
    
    CURRENT DESCRIPTION:
    "{description}"
    
    TASK:
    1. Determine if the description accurately describes THIS specific book. 
    2. If it describes a different book (e.g., describes Peter Pan but the title is Dracula), it is a MISMATCH.
    3. If it is generic or vague but not wrong, it is a MATCH.
    
    OUTPUT JSON ONLY:
    {{
        "match": true/false,
        "reason": "Short explanation",
        "corrected_blurb": "If match is false, write a correct 2-sentence hook here. Otherwise null."
    }}
    """
    
    try:
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        data = json.loads(response.text)
        return data.get("match"), data.get("reason"), data.get("corrected_blurb")
    except Exception as e:
        log(f"   [AI Error] {e}")
        return None, str(e), None

def run_audit(auto_fix=False):
    log("--- Starting Library Metadata Audit ---")
    
    # Only check standard books that actually HAVE a description
    books = list(db.books.find({
        "edition": "std",
        "description": {"$exists": True, "$ne": ""}
    }))
    
    issues_found = 0
    
    for book in books:
        title = book['title']
        desc = book['description']
        
        # Skip if description is very short/placeholder
        if len(desc) < 10: continue

        is_match, reason, fix = check_consistency(title, book['author'], desc)
        
        if is_match is False:
            issues_found += 1
            log(f"\n[MISMATCH DETECTED] {title}")
            log(f"   Current: {desc}")
            log(f"   AI Reason: {reason}")
            
            if auto_fix and fix:
                log(f"   -> AUTO-FIXING with: {fix}")
                # Update all editions
                base_id = book['book_id'].replace("_short", "").replace("_long", "")
                db.books.update_many(
                    {"book_id": {"$regex": f"^{base_id}"}},
                    {"$set": {"description": fix}}
                )
            elif fix:
                log(f"   -> Suggested Fix: {fix}")
        else:
            print(".", end="", flush=True) # Progress dot for good books
            
        time.sleep(0.5) # Rate limit protection

    log(f"\n\n--- Audit Complete. Found {issues_found} issues. ---")

if __name__ == "__main__":
    # Run with auto_fix=True if you trust the AI, or False to just see logs
    # To enable auto-fixing, change this to True
    run_audit(auto_fix=True)
