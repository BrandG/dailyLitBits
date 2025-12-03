import requests
import re
import argparse
import sys
import os
from pymongo import MongoClient
import config
import time

# --- SETUP ---
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

def get_gutenberg_text(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        response.encoding = 'utf-8-sig'
        return response.text
    except Exception as e:
        print(f"Error fetching URL: {e}")
        return None

def derive_metadata(source):
    if source.startswith("http"):
        url = source
        match = re.search(r'/pg(\d+)\.txt', url)
        book_id = f"pg{match.group(1)}" if match else None
        return url, book_id

    clean_id = source.lower().replace("pg", "")
    if clean_id.isdigit():
        url = f"https://www.gutenberg.org/cache/epub/{clean_id}/pg{clean_id}.txt"
        book_id = f"pg{clean_id}"
        return url, book_id

    raise ValueError(f"Could not interpret source '{source}'")

def extract_title(text):
    match = re.search(r'^Title:\s+(.+)$', text, re.MULTILINE)
    return match.group(1).strip() if match else "Unknown Title"

def clean_text(text):
    start_markers = [r"\*\*\* START OF .* \*\*\*", r"START OF THE PROJECT GUTENBERG EBOOK"]
    end_markers = [r"\*\*\* END OF .* \*\*\*", r"End of the Project Gutenberg EBook"]
    
    start_pos = 0
    for marker in start_markers:
        match = re.search(marker, text, re.IGNORECASE)
        if match:
            start_pos = match.end(); break
            
    end_pos = len(text)
    for marker in end_markers:
        match = re.search(marker, text, re.IGNORECASE)
        if match:
            end_pos = match.start(); break
            
    return text[start_pos:end_pos].strip()

def chunk_text(text, title, book_id, source_url):
    # Check if book exists
    if db.books.find_one({"book_id": book_id}):
        print(f"Book '{book_id}' exists. Deleting old version to re-ingest...")
        db.books.delete_one({"book_id": book_id})
        db.chunks.delete_many({"book_id": book_id})

    text = text.replace('\r\n', '\n').replace('\r', '\n')
    paragraphs = text.split('\n\n')
    
    chunks_collection = db['chunks']
    print(f"Processing '{title}' (ID: {book_id})...")

    memory_chunks = []
    current_chunk = []
    current_word_count = 0
    sequence = 1
    
    for para in paragraphs:
        clean_para = para.strip()
        if not clean_para: continue
            
        word_count = len(clean_para.split())
        
        if current_word_count + word_count > 1000 and current_chunk:
            chunk_content = "\n\n".join(current_chunk)
            
            # Note: recap is None for now. The summarize.py worker will fill it later.
            memory_chunks.append({
                "book_id": book_id,
                "sequence": sequence,
                "content": chunk_content,
                "word_count": current_word_count,
                "recap": None, 
            })
            
            sequence += 1
            current_chunk = []
            current_word_count = 0
            
        current_chunk.append(clean_para)
        current_word_count += word_count

    if current_chunk:
        chunk_content = "\n\n".join(current_chunk)
        memory_chunks.append({
            "book_id": book_id,
            "sequence": sequence,
            "content": chunk_content,
            "word_count": current_word_count,
            "recap": None,
        })

    # --- COMMIT ---
    if memory_chunks:
        db.chunks.insert_many(memory_chunks)

    db.books.insert_one({
        "book_id": book_id,
        "title": title,
        "total_chunks": sequence,
        "source_url": source_url
    })
    print(f"Success! '{title}' ingested. ({sequence} chunks). Ready for AI processing.")

def process_source(source, override_title=None, override_id=None):
    try:
        url, derived_id = derive_metadata(source)
        book_id = override_id if override_id else derived_id
        if not book_id:
            print(f"Skipping '{source}': Could not determine Book ID.")
            return

        print(f"Fetching: {url}")
        raw_text = get_gutenberg_text(url)
        if not raw_text:
            print(f"Skipping '{source}': Download failed.")
            return

        title = override_title if override_title else extract_title(raw_text)
        clean_content = clean_text(raw_text)
        chunk_text(clean_content, title, book_id, url)
    except Exception as e:
        print(f"Error processing '{source}': {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest books into DailyLitBits (Fast Mode - No AI)")
    parser.add_argument("source", help="Gutenberg ID, URL, or path to .txt file list")
    parser.add_argument("--title", "-t", help="Override Title (Single mode only)")
    parser.add_argument("--id", "-i", help="Override ID (Single mode only)")
    args = parser.parse_args()

    # BULK MODE
    if os.path.isfile(args.source):
        print(f"--- Bulk Ingest Mode: Reading from {args.source} ---")
        with open(args.source, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    process_source(line)
                    # Tiny sleep just to be polite to Gutenberg servers
                    time.sleep(0.5) 
    else:
        # SINGLE MODE
        process_source(args.source, args.title, args.id)
