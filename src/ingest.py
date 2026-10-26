import requests
import re
import argparse
import sys
import os
import shutil
from google import genai
from datetime import datetime, timezone
import config
import time
import ai
import db

# --- AI & PATH CONFIGURATION ---
COVER_DIR = "static/covers"
GENAI_MODEL_NAME = config.GEMINI_MODEL

ai_client = None
if config.GEMINI_API_KEY:
    ai_client = genai.Client(api_key=config.GEMINI_API_KEY)


# --- CONFIGURATION ---
# Every book is chunked once, at this size. Readers choose how many chunks
# arrive per day (subscriptions.chunks_per_day), so this is the smallest
# unit of delivery rather than one of three stored editions.
CHUNK_TARGET_WORDS = 325


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

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
    match = re.search(r'Title:\s+(.+)$', text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else "Unknown Title"

def extract_author(text):
    match = re.search(r'Author:\s+(.+)$', text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else "Unknown Author"

def clean_text(text):
    start_markers = [
        r"\
* ?START OF (THE|THIS) PROJECT GUTENBERG.*",
        r"START OF THE PROJECT GUTENBERG",
        r"start of the project gutenberg",
    ]
    end_markers = [
        r"\
* ?END OF (THE|THIS) PROJECT GUTENBERG.*",
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
    if not ai_client:
        return None

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
        response = ai_client.models.generate_content(
            model=GENAI_MODEL_NAME,
            contents=prompt
        )
        blurb = response.text.strip()
        print(f"        -> {blurb}")
        time.sleep(1.0)
        return blurb
    except Exception as e:
        print(f"   [AI] Error generating blurb: {e}")
        return None


# --- CORE CHUNKING LOGIC ---
def split_long_paragraph(paragraph, target_words):
    """Splits a paragraph that is itself longer than a whole chunk.

    Gutenberg texts carry the occasional 2,000-word 'paragraph' - contents
    blocks, unbroken letters, runs of verse. Splitting on blank lines alone
    cannot honour a 325-word target, so oversized paragraphs are broken on
    sentence endings instead. A single sentence longer than the target is
    left alone; mid-sentence breaks read worse than a long chunk.
    """
    sentences = re.split(r'(?<=[.!?])\s+', paragraph)
    pieces = []
    current = []
    count = 0

    for sentence in sentences:
        words = sentence.split()

        # A single "sentence" can still dwarf a chunk. Ulysses' last episode
        # has no sentence-ending punctuation at all, which produced 4,400-word
        # deliveries in a 325-word scheme. When there is no sentence boundary
        # to use, split on word count: a mid-sentence break reads poorly, but
        # it is the only break available and it beats sending eight chunks as
        # one. The 2x threshold leaves genuinely long sentences intact.
        if len(words) > target_words * 2:
            if current:
                pieces.append(" ".join(current))
                current = []
                count = 0
            for start in range(0, len(words), target_words):
                pieces.append(" ".join(words[start:start + target_words]))
            continue

        if current and count + len(words) > target_words:
            pieces.append(" ".join(current))
            current = []
            count = 0

        current.append(sentence)
        count += len(words)

    if current:
        pieces.append(" ".join(current))

    return pieces


def create_chunks(paragraphs, target_words=CHUNK_TARGET_WORDS):
    """Splits paragraphs into chunks of roughly target_words.

    Keeps the original rule: only break when adding the next paragraph would
    overflow AND the chunk already carries its weight, so we never emit a
    two-line fragment just because a long paragraph is coming.
    """
    # Break up anything already bigger than a chunk before grouping.
    units = []
    for para in paragraphs:
        clean_para = para.strip()
        if not clean_para:
            continue
        if len(clean_para.split()) > target_words:
            units.extend(split_long_paragraph(clean_para, target_words))
        else:
            units.append(clean_para)

    chunks = []
    current_chunk = []
    current_word_count = 0
    sequence = 1

    for clean_para in units:
        word_count = len(clean_para.split())

        will_overflow = (current_word_count + word_count > target_words)
        is_substantial = (current_word_count > (target_words * 0.5))

        if will_overflow and is_substantial and current_chunk:
            chunks.append({
                "sequence": sequence,
                "content": "\n\n".join(current_chunk),
                "word_count": current_word_count,
            })
            sequence += 1
            current_chunk = []
            current_word_count = 0

        current_chunk.append(clean_para)
        current_word_count += word_count

    if current_chunk:
        chunks.append({
            "sequence": sequence,
            "content": "\n\n".join(current_chunk),
            "word_count": current_word_count,
        })

    return chunks


def generate_recaps_for_book(conn, book_id, limit=None):
    """Generates the chained 'story so far' recap for each chunk of a book.

    Chunk N's recap covers everything up to and including chunk N, so a
    delivery starting at chunk S reads chunk S-1's recap whatever
    chunks_per_day is set to. The chain is the point and also the cost: the
    calls cannot be parallelised within a book, and every chunk needs one.
    That is why this never runs as part of a plain ingest.

    Chunks that already have a recap are skipped without an API call, so
    re-running is cheap and this doubles as the resume path.

    A chunk the model will not summarise is left NULL and the previous
    context carries forward. Gemini's PROHIBITED_CONTENT filter rejects some
    passages of classic literature outright, and it is not configurable - so
    stopping the book at the first refusal (which this used to do) left 1,256
    chunks of War and Peace unprocessed behind one blocked passage. A missing
    recap costs the reader the "Previously:" block on one delivery; a stopped
    chain costs them the rest of the book.
    """
    rows = conn.execute(
        "SELECT sequence, content, recap FROM chunks WHERE book_id = ? ORDER BY sequence",
        (book_id,),
    ).fetchall()

    if not rows:
        print(f"   [Recaps] No chunks stored for '{book_id}' - ingest it first.")
        return 0, 0.0

    missing = sum(1 for row in rows if row["recap"] is None)
    if not missing:
        print(f"   [Recaps] '{book_id}' already complete ({len(rows)} chunks).")
        return 0, 0.0

    print(f"   [Recaps] {missing} of {len(rows)} chunks need a recap for '{book_id}'...")
    previous_recap = None
    calls = 0
    blocked = 0
    started = time.time()

    for row in rows:
        # Already done: carry its context forward, spend nothing.
        if row["recap"]:
            previous_recap = row["recap"]
            continue

        if limit is not None and calls >= limit:
            print(f"   [Recaps] Reached the --limit of {limit} calls.")
            break

        summary = ai.generate_recap(row["content"], previous_recap=previous_recap)
        calls += 1

        if not summary:
            blocked += 1
            print(f"   [Recaps] Chunk {row['sequence']} left without a recap; "
                  f"carrying the previous context forward.")
            continue

        conn.execute(
            "UPDATE chunks SET recap = ? WHERE book_id = ? AND sequence = ?",
            (summary, book_id, row["sequence"]),
        )
        previous_recap = summary

        if calls % 25 == 0:
            print(f"   [Recaps] {calls} calls, {time.time() - started:.0f}s elapsed...")

    elapsed = time.time() - started
    per_call = elapsed / calls if calls else 0
    print(f"   [Recaps] {calls} Gemini calls in {elapsed:.1f}s ({per_call:.2f}s per call)"
          + (f", {blocked} chunk(s) refused by the content filter." if blocked else "."))
    return calls, elapsed


def ingest_book(text, title, author, book_id, source_url,
                use_ai=True, with_recaps=False):
    # Prepare text
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    paragraphs = text.split('\n\n')

    print(f"Processing '{title}' (ID: {book_id})...")

    cover_url = download_cover(book_id)
    blurb = generate_blurb(title, author) if use_ai else None

    chunks = create_chunks(paragraphs)
    if not chunks:
        print(f"   [!] No text found in '{title}' - nothing stored.")
        return

    total_words = sum(c["word_count"] for c in chunks)

    conn = db.get_db()
    try:
        with db.transaction(conn):
            # Re-ingesting replaces the book; the chunks go with it via
            # ON DELETE CASCADE.
            conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
            conn.execute(
                """INSERT INTO books (id, title, author, blurb, cover_path,
                                      source_url, total_chunks, total_words,
                                      created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (book_id, title, author, blurb, cover_url, source_url,
                 len(chunks), total_words, utc_now()),
            )
            conn.executemany(
                """INSERT INTO chunks (book_id, sequence, content, word_count)
                   VALUES (?, ?, ?, ?)""",
                [(book_id, c["sequence"], c["content"], c["word_count"])
                 for c in chunks],
            )

        print(f"  -> {len(chunks)} chunks, {total_words:,} words "
              f"({CHUNK_TARGET_WORDS} words/chunk target)")

        if with_recaps:
            generate_recaps_for_book(conn, book_id)
        else:
            print("  -> Recaps not generated. Backfill later with --recaps-only.")
    finally:
        conn.close()

    print(f"Success! '{title}' ingested.")


def process_source(source, override_title=None, override_author=None,
                   override_id=None, use_ai=True, with_recaps=False):
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
        ingest_book(clean_content, title, author, book_id, url,
                    use_ai=use_ai, with_recaps=with_recaps)
        
    except Exception as e:
        print(f"Error processing '{source}': {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest books into dailyLitBits (SQLite, single chunk size)")
    parser.add_argument("source", help="Gutenberg ID, URL, or path to a list file")
    parser.add_argument("--title", "-t", help="Override Title (single mode only)")
    parser.add_argument("--author", "-a", help="Override Author (single mode only)")
    parser.add_argument("--id", "-i", help="Override ID (single mode only)")
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip the blurb call too, so ingest makes no API calls")
    parser.add_argument("--recaps", action="store_true",
                        help="Also generate chained recaps: one Gemini call per "
                             "chunk, sequential. Off by default because a whole "
                             "library is tens of thousands of calls.")
    parser.add_argument("--blurb-only", action="store_true",
                        help="Regenerate just the library blurb for a book already "
                             "stored. No download, no re-chunking, keeps recaps.")
    parser.add_argument("--recaps-only", action="store_true",
                        help="Backfill recaps for a book already in the database. "
                             "No download, no re-chunking.")
    parser.add_argument("--limit", type=int,
                        help="With --recaps/--recaps-only, stop after N chunks. "
                             "Useful for measuring what a full run would cost.")
    args = parser.parse_args()

    if args.blurb_only:
        book_id = args.id if args.id else derive_metadata(args.source)[1]
        conn = db.get_db()
        try:
            row = conn.execute(
                "SELECT id, title, author FROM books WHERE id = ?", (book_id,)).fetchone()
            if not row:
                print(f"'{book_id}' is not in the database - ingest it first.")
                sys.exit(1)
            blurb = generate_blurb(row["title"], row["author"])
            if not blurb:
                print("No blurb returned.")
                sys.exit(1)
            conn.execute("UPDATE books SET blurb = ? WHERE id = ?", (blurb, book_id))
            print(f"Updated the blurb for {book_id}.")
        finally:
            conn.close()
        sys.exit(0)

    if args.recaps_only:
        book_id = args.id if args.id else derive_metadata(args.source)[1]
        conn = db.get_db()
        try:
            generate_recaps_for_book(conn, book_id, limit=args.limit)
        finally:
            conn.close()
        sys.exit(0)

    # BULK MODE
    if os.path.isfile(args.source):
        print(f"--- Bulk Ingest Mode: Reading from {args.source} ---")
        with open(args.source, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    process_source(line, use_ai=not args.no_ai,
                                   with_recaps=args.recaps)
                    time.sleep(1.0)
    else:
        # SINGLE MODE
        process_source(args.source, args.title, args.author, args.id,
                       use_ai=not args.no_ai, with_recaps=args.recaps)
