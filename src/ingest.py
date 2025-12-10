import requests
import re
import argparse
import sys
import os
import shutil
import google.generativeai as genai
from pymongo import MongoClient
import config
import time

# --- SETUP ---
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

# --- AI & PATH CONFIGURATION ---
COVER_DIR = "static/covers"
GENAI_MODEL_NAME = 'models/gemini-flash-latest' 

if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)


# --- CONFIGURATION ---
EDITION_CONFIG = {
    "std":   {"suffix": "",       "words": 750},  # Standard (No suffix)
    "short": {"suffix": "_short", "words": 325},  # Short
    "long":  {"suffix": "_long",  "words": 1500}  # Long
}

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

def extract_author(text):
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

    for i, line in enumerate(lines[:300]):
        for marker in start_markers:
            if re.search(marker, line, re.IGNORECASE):
                start_idx = i + 1
                break
        if start_idx > 0: break

    for i, line in enumerate(lines[::-1][:300]):
        for marker in end_markers:
            if re.search(marker, line, re.IGNORECASE):
                end_idx = len(lines) - i - 1
                break
        if end_idx < len(lines): break

    return "\n".join(lines[start_idx:end_idx]).strip()

def download_cover(book_id):
    """
    Downloads the cover image from Project Gutenberg and saves it locally.
    Returns the local relative path (e.g., '/static/covers/11.jpg') or None.
    """
    os.makedirs(COVER_DIR, exist_ok=True)
    clean_id = book_id.lower().replace("pg", "").replace("_short", "").replace("_long", "")
    
    if not clean_id.isdigit():
        return None
    
    remote_url = f"https://www.gutenberg.org/cache/epub/{clean_id}/pg{clean_id}.cover.medium.jpg"
    filename = f"{clean_id}.jpg"
    local_path = os.path.join(COVER_DIR, filename)
    public_path = f"/{COVER_DIR}/{filename}"
    
    if os.path.exists(local_path):
        return public_path
        
    try:
        headers = {'User-Agent': 'DailyLitBits-Indexer/1.0'}
        response = requests.get(remote_url, headers=headers, stream=True, timeout=10)
        
        if response.status_code == 200:
            with open(local_path, 'wb') as f:
                response.raw.decode_content = True
                shutil.copyfileobj(response.raw, f)
            print(f"   [Cover] Downloaded: {filename}")
            time.sleep(0.5)
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
        print(f"   [AI] Generating blurb for '{title}'...")
        response = model.generate_content(prompt)
        blurb = response.text.strip()
        print(f"        -> {blurb}")
        time.sleep(1.0)
        return blurb
    except Exception as e:
        print(f"   [AI] Error generating blurb: {e}")
        return None


# --- CORE CHUNKING LOGIC ---
def create_edition_chunks(paragraphs, unique_book_id, edition_name, target_words):
    memory_chunks = []
    current_chunk = []
    current_word_count = 0
    sequence = 1
    
    for para in paragraphs:
        clean_para = para.strip()
        if not clean_para: continue
            
        word_count = len(clean_para.split())
        
        # 1. Will adding this paragraph blow the limit?
        will_overflow = (current_word_count + word_count > target_words)
        
        # 2. Is the current chunk "meaty" enough to stand alone? (e.g. > 50% of target)
        is_substantial = (current_word_count > (target_words * 0.5))
        
        # Only split if BOTH are true
        if will_overflow and is_substantial and current_chunk:
            chunk_content = "\n\n".join(current_chunk)
            
            memory_chunks.append({
                "book_id": unique_book_id, # This is now unique (e.g. pg11_short)
                "sequence": sequence,
                "edition": edition_name,
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
            "book_id": unique_book_id,
            "sequence": sequence,
            "edition": edition_name,
            "content": "\n\n".join(current_chunk),
            "word_count": current_word_count,
            "recap": None,
        })
        
    return memory_chunks, sequence

def ingest_book(text, title, author, base_book_id, source_url):
    # Prepare text
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    paragraphs = text.split('\n\n')
    
    print(f"Processing '{title}' (Base ID: {base_book_id})...")
    
    # --- ENHANCEMENT ---
    # Generate cover and blurb once for the base book
    cover_url = download_cover(base_book_id)
    description = generate_blurb(title, author)
    
    # Generate Editions
    for edition_name, conf in EDITION_CONFIG.items():
        suffix = conf["suffix"]
        target_words = conf["words"]
        
        # 1. Determine Unique ID (e.g., pg11, pg11_short, pg11_long)
        unique_book_id = f"{base_book_id}{suffix}"
        
        # 2. Cleanup Old Data for this specific edition
        if db.books.find_one({"book_id": unique_book_id}):
            db.books.delete_one({"book_id": unique_book_id})
            db.chunks.delete_many({"book_id": unique_book_id})

        print(f"  -> Generating '{edition_name}' edition ({target_words} words) as ID: {unique_book_id}...")
        
        chunks, total_chunks = create_edition_chunks(paragraphs, unique_book_id, edition_name, target_words)
        
        # 3. Insert Chunks
        if chunks:
            db.chunks.insert_many(chunks)

        # 4. Insert Book Metadata (Separate entry for each edition)
        book_doc = {
            "book_id": unique_book_id,
            "parent_id": base_book_id, # Link them together if needed later
            "title": title,
            "author": author,
            "source_url": source_url,
            "total_chunks": total_chunks,
            "edition": edition_name,
            "chunk_size": target_words,
            "cover_url": cover_url,
            "description": description
        }
        db.books.insert_one(book_doc)

    print(f"Success! '{title}' ingested and enhanced.")

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
        author = override_author if override_author else extract_author(raw_text)
        
        clean_content = clean_text(raw_text)
        ingest_book(clean_content, title, author, book_id, url)
        
    except Exception as e:
        print(f"Error processing '{source}': {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest books into DailyLitBits (Multi-Edition Support)")
    parser.add_argument("source", help="Gutenberg ID, URL, or path to .txt file list")
    parser.add_argument("--title", "-t", help="Override Title (Single mode only)")
    parser.add_argument("--author", "-a", help="Override Author (Single mode only)")
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
                    time.sleep(1.0) 
    else:
        # SINGLE MODE
        process_source(args.source, args.title, args.author, args.id)