from logger import log
import time
from pymongo import MongoClient
import config
import ai

client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

def backfill_recaps():
    log(f"--- Starting AI Backfill Worker ---")
    
    # REMOVED .limit(50) so it does everything
    query = {"sequence": {"$gt": 1}, "recap": None}
    # Using a cursor instead of list() prevents loading 72k objects into RAM at once
    cursor = db.chunks.find(query) 
    
    log(f"Found {db.chunks.count_documents(query)} chunks needing recaps.")
    
    count = 0
    for chunk in cursor:
        book_id = chunk['book_id']
        seq = chunk['sequence']
        
        prev_seq = seq - 1
        prev_chunk = db.chunks.find_one({"book_id": book_id, "sequence": prev_seq})
        
        if not prev_chunk:
            continue
            
        context_summary = prev_chunk.get('recap')
        
        # Log less frequently to keep terminal clean
        if count % 100 == 0:
            log(f"[{count}] Processing {book_id} part {seq}...")
        
        summary = ai.generate_recap(prev_chunk['content'], previous_recap=context_summary)
        
        if summary:
            db.chunks.update_one(
                {"_id": chunk['_id']},
                {"$set": {"recap": summary}}
            )
        else:
            # CHANGED: Don't stop, just log and move on
            log(f"   [!] Failed to generate for {book_id} {seq}. Skipping.")
        
        count += 1
        # No sleep needed for Tier 1

    log("--- Backfill Run Complete ---")

if __name__ == "__main__":
    backfill_recaps()
