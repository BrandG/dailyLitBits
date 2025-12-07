import time
from pymongo import MongoClient
import config
import ai

# Setup
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

def backfill_recaps():
    print(f"--- Starting AI Backfill Worker: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    # Find chunks that are missing recaps (skip sequence 1)
    # We limit to 50 to prevent blowing the entire daily quota in one run
    query = {"sequence": {"$gt": 1}, "recap": None}
    to_process = list(db.chunks.find(query).limit(50))
    
    print(f"Found {len(to_process)} chunks needing recaps.")
    
    for chunk in to_process:
        book_id = chunk['book_id']
        seq = chunk['sequence']
        
        # We need to summarize the PREVIOUS chunk (Seq - 1)
        prev_seq = seq - 1
        prev_chunk = db.chunks.find_one({"book_id": book_id, "sequence": prev_seq})
        
        if not prev_chunk:
            continue
            
        # Get the context (The recap stored on Seq - 1, which summarizes Seq - 2)
        context_summary = prev_chunk.get('recap')
        
        print(f"Processing {book_id} part {seq} (Context available: {context_summary is not None})...")
        
        # Generate
        summary = ai.generate_recap(prev_chunk['content'], previous_recap=context_summary)
        
        if summary:
            db.chunks.update_one(
                {"_id": chunk['_id']},
                {"$set": {"recap": summary}}
            )
            print(f"   -> Saved.")
            time.sleep(2) 
        else:
            print(f"   -> Failed. Stopping run.")
            break

    print("--- Backfill Run Complete ---")

if __name__ == "__main__":
    backfill_recaps()