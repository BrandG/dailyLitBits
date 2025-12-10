import time
import concurrent.futures
from pymongo import MongoClient
import config
import ai

# --- CONFIG ---
MAX_WORKERS = 10  # Number of simultaneous books to process

client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

def process_book(book_id):
    """
    Processes a single book strictly in order to maintain narrative context.
    """
    # 1. Find all chunks for this book that are missing a recap
    # We MUST sort by sequence ASC so we generate them in order (1, 2, 3...)
    query = {"book_id": book_id, "recap": None, "sequence": {"$gt": 1}}
    chunks_to_fix = list(db.chunks.find(query).sort("sequence", 1))
    
    if not chunks_to_fix:
        return 0

    print(f"[{book_id}] Starting batch ({len(chunks_to_fix)} chunks)...")
    count = 0

    for chunk in chunks_to_fix:
        seq = chunk['sequence']
        
        # 2. Get Context (The recap of the PREVIOUS chunk)
        # Since we are running in order, the DB should have the previous one ready.
        prev_seq = seq - 1
        prev_chunk = db.chunks.find_one({"book_id": book_id, "sequence": prev_seq})
        
        # Safety: If prev chunk is missing or has no recap (and it's not seq 1),
        # we have a gap in the chain. We can either skip or run without context.
        # Here we run without context to ensure we don't get stuck forever.
        context_summary = prev_chunk.get('recap') if prev_chunk else None
        
        # 3. Generate
        summary = ai.generate_recap(prev_chunk['content'], previous_recap=context_summary)
        
        if summary:
            db.chunks.update_one(
                {"_id": chunk['_id']},
                {"$set": {"recap": summary}}
            )
            count += 1
        else:
            print(f"   [!] Failed to generate for {book_id} seq {seq}")

    print(f"[{book_id}] Finished batch. Updated {count} chunks.")
    return count

def main():
    start = time.time()
    print(f"--- Starting Multi-Threaded Backfill (Workers={MAX_WORKERS}) ---")
    
    # 1. Identify all books that need work
    # We use distinct() to get a list of unique book_IDs that have at least one null recap
    print("Scanning DB for incomplete books...")
    book_ids = db.chunks.distinct("book_id", {"recap": None, "sequence": {"$gt": 1}})
    
    print(f"Found {len(book_ids)} books with missing recaps.")
    
    total_fixed = 0
    
    # 2. Fire up the Thread Pool
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all books to the pool
        future_to_book = {executor.submit(process_book, bid): bid for bid in book_ids}
        
        for future in concurrent.futures.as_completed(future_to_book):
            book_id = future_to_book[future]
            try:
                count = future.result()
                total_fixed += count
            except Exception as exc:
                print(f"[{book_id}] Generated an exception: {exc}")

    duration = time.time() - start
    print("="*40)
    print(f"JOB COMPLETE.")
    print(f"Processed {total_fixed} chunks in {duration:.2f} seconds.")
    print(f"Average Speed: {total_fixed / duration * 60:.2f} RPM")
    print("="*40)

if __name__ == "__main__":
    main()
