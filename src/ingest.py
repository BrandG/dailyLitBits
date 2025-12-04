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
        response.encoding = response.apparent_encoding
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

# --- NEW FUNCTION ---
def extract_author(text):
    # Regex to find "Author: [Name]"
    match = re.search(r'^Author:\s+(.+)$', text, re.MULTILINE)
    return match.group(1).strip() if match else "Unknown Author"

def clean_text(text):
    start_markers = [
        r"\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG.*",
        r"START OF THE PROJECT GUTENBERG",
        r"start of the project gutenberg",
    ]
    end_markers = [
        r"\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG.*",
        r"End of (The )?Project Gutenberg",
        r"End of the project gutenberg",
    ]

    lines = text.splitlines()
    start_idx = 0
    end_idx = len(lines)

    # Scan for start
    for i, line in enumerate(lines[:300]): # Only check first 300 lines
        for marker in start_markers:
            if re.search(marker, line, re.IGNORECASE):
                start_idx = i + 1
                break
        if start_idx > 0: break

    # Scan for end (reverse)
    for i, line in enumerate(lines[::-1][:300]): # Only check last 300 lines
        for marker in end_markers:
            if re.search(marker, line, re.IGNORECASE):
                end_idx = len(lines) - i - 1
                break
        if end_idx < len(lines): break

    # Rejoin and return
    return "\n".join(lines[start_idx:end_idx]).strip()

# --- UPDATED ARGUMENTS ---
def chunk_text(text, title, author, book_id, source_url):
    # Check if book exists
    if db.books.find_one({"book_id": book_id}):
        print(f"Book '{book_id}' exists. Deleting old version to re-ingest...")
        db.books.delete_one({"book_id": book_id})
        db.chunks.delete_many({"book_id": book_id})

    text = text.replace('\r\n', '\n').replace('\r', '\n')
    paragraphs = text.split('\n\n')
    
    print(f"Processing '{title}' by {author} (ID: {book_id})...")

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

    # Insert with Author field
    db.books.insert_one({
        "book_id": book_id,
        "title": title,
        "author": author,  # <--- NEW FIELD
        "total_chunks": sequence,
        "source_url": source_url
    })
    print(f"Success! '{title}' ingested. ({sequence} chunks). Ready for AI processing.")

def process_source(source, override_title=None, override_author=None, override_id=None):
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
        author = override_author if override_author else extract_author(raw_text) # <--- Extract Author
        
        clean_content = clean_text(raw_text)
        chunk_text(clean_content, title, author, book_id, url)
    except Exception as e:
        print(f"Error processing '{source}': {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest books into DailyLitBits (Fast Mode - No AI)")
    parser.add_argument("source", help="Gutenberg ID, URL, or path to .txt file list")
    parser.add_argument("--title", "-t", help="Override Title (Single mode only)")
    parser.add_argument("--author", "-a", help="Override Author (Single mode only)") # <--- Add Argument
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
                    time.sleep(0.5) 
    else:
        # SINGLE MODE
        process_source(args.source, args.title, args.author, args.id)
