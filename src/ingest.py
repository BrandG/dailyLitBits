import requests
import re
import argparse
import sys
from pymongo import MongoClient
import config

# Setup Mongo Connection using Config
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]

def get_gutenberg_text(url):
    try:
        response = requests.get(url)
        response.raise_for_status() # Raise error if 404/500
        response.encoding = 'utf-8-sig' # Handle BOM if present
        return response.text
    except Exception as e:
        print(f"Error fetching URL: {e}")
        sys.exit(1)

def derive_metadata(source):
    """
    Intelligently figures out the URL and Book ID from a single input string.
    Input can be:
      - A URL: "https://www.gutenberg.org/cache/epub/84/pg84.txt"
      - A raw number: "84"
      - A pg-ID: "pg84"
    """
    # 1. Handle URL
    if source.startswith("http://") or source.startswith("https://"):
        url = source
        # Try to regex the ID out of the filename (e.g., pg84.txt -> pg84)
        match = re.search(r'/pg(\d+)\.txt', url)
        if match:
            book_id = f"pg{match.group(1)}"
        else:
            # Fallback: create a hash or require manual ID if URL is weird
            book_id = None 
        return url, book_id

    # 2. Handle ID (Number or String)
    # Strip "pg" prefix if present to get the number
    clean_id = source.lower().replace("pg", "")
    
    if clean_id.isdigit():
        # Construct standard Gutenberg URL pattern
        url = f"https://www.gutenberg.org/cache/epub/{clean_id}/pg{clean_id}.txt"
        book_id = f"pg{clean_id}"
        return url, book_id

    # 3. Invalid
    raise ValueError(f"Could not interpret source '{source}'. Must be a URL or a Gutenberg ID.")

def extract_title(text):
    """
    Scrapes the 'Title: ...' line from the Gutenberg header.
    """
    # Regex looks for "Title: [Something]" at the start of a line
    match = re.search(r'^Title:\s+(.+)$', text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return "Unknown Title"

def clean_text(text):
    start_markers = [
        r"\*\*\* START OF (THE|THIS) PROJECT GUTENBERG EBOOK .* \*\*\*",
        r"\*\*\* START OF THE PROJECT GUTENBERG EBOOK",
        r"START OF THE PROJECT GUTENBERG EBOOK",
    ]
    end_markers = [
        r"\*\*\* END OF (THE|THIS) PROJECT GUTENBERG EBOOK .* \*\*\*",
        r"\*\*\* END OF THE PROJECT GUTENBERG EBOOK",
        r"End of the Project Gutenberg EBook",
    ]

    start_pos = 0
    for marker in start_markers:
        match = re.search(marker, text, re.IGNORECASE)
        if match:
            start_pos = match.end()
            break
            
    end_pos = len(text)
    for marker in end_markers:
        match = re.search(marker, text, re.IGNORECASE)
        if match:
            end_pos = match.start()
            break
            
    return text[start_pos:end_pos].strip()

def chunk_text(text, title, book_id, source_url):
    # Check for duplicates
    if db.books.find_one({"book_id": book_id}):
        print(f"Error: Book ID '{book_id}' already exists in the library.")
        return

    # Normalize Line Endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    paragraphs = text.split('\n\n')
    chunks = []
    current_chunk = []
    current_word_count = 0
    sequence = 1
    
    print(f"Processing '{title}' (ID: {book_id})...")

    chunks_collection = db['chunks']

    for para in paragraphs:
        clean_para = para.strip()
        if not clean_para:
            continue
            
        word_count = len(clean_para.split())
        
        if current_word_count + word_count > 1000 and current_chunk:
            chunk_content = "\n\n".join(current_chunk)
            
            chunk_doc = {
                "book_id": book_id,
                "sequence": sequence,
                "content": chunk_content,
                "word_count": current_word_count
            }
            chunks_collection.insert_one(chunk_doc)
            
            sequence += 1
            current_chunk = []
            current_word_count = 0
            
        current_chunk.append(clean_para)
        current_word_count += word_count

    if current_chunk:
        chunk_doc = {
            "book_id": book_id,
            "sequence": sequence,
            "content": "\n\n".join(current_chunk),
            "word_count": current_word_count
        }
        chunks_collection.insert_one(chunk_doc)

    db['books'].insert_one({
        "book_id": book_id,
        "title": title,
        "total_chunks": sequence,
        "source_url": source_url
    })
    
    print(f"Success! '{title}' is ready. Total daily chunks: {sequence}.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest a book into DailyLitBits")
    
    # The 'source' can now be an ID OR a URL
    parser.add_argument("source", help="Gutenberg ID (e.g. 84) OR full URL")
    
    # Title and ID are now optional overrides
    parser.add_argument("--title", "-t", help="Override Book Title (defaults to auto-detect)")
    parser.add_argument("--id", "-i", help="Override Book ID (defaults to auto-detect)")

    args = parser.parse_args()

    try:
        # 1. Figure out URL and ID
        url, derived_id = derive_metadata(args.source)
        
        # Allow manual ID override, otherwise use derived
        book_id = args.id if args.id else derived_id
        
        if not book_id:
            print("Error: Could not determine Book ID. Please specify --id manually.")
            sys.exit(1)

        # 2. Download
        print(f"Fetching: {url}")
        raw_text = get_gutenberg_text(url)
        
        # 3. Figure out Title
        title = args.title if args.title else extract_title(raw_text)
        
        # 4. Process
        clean_content = clean_text(raw_text)
        chunk_text(clean_content, title, book_id, url)

    except ValueError as e:
        print(f"Input Error: {e}")
        sys.exit(1)
