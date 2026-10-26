import pytest
import os
import sys

# --- PATH SETUP ---
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
import config

import ingest
from ingest import clean_text, extract_title, extract_author, split_long_paragraph, create_chunks

def test_clean_text_basic():
    text = """
    *** START OF THE PROJECT GUTENBERG EBOOK ANNA KARENINA ***

    This is the first paragraph.

    This is the second paragraph.

    *** END OF THE PROJECT GUTENBERG EBOOK ANNA KARENINA ***
    """
    expected = "This is the first paragraph.\n\n    This is the second paragraph."
    assert clean_text(text) == expected

def test_clean_text_different_markers():
    text = """
    START OF THIS PROJECT GUTENBERG

    Content here.

    End of The Project Gutenberg
    """
    expected = "Content here."
    assert clean_text(text) == expected

def test_clean_text_no_markers():
    text = """
    Just some text.

    No markers here.
    """
    expected = "Just some text.\n\n    No markers here."
    assert clean_text(text) == expected

def test_clean_text_with_leading_trailing_whitespace():
    text = """
    \n\n    *** START OF THE PROJECT GUTENBERG EBOOK ***\n\n    Leading and trailing whitespace.\n
    *** END OF THE PROJECT GUTENBERG EBOOK ***\n\n    """
    expected = "Leading and trailing whitespace."
    assert clean_text(text) == expected

def test_clean_text_empty_input():
    text = ""
    expected = ""
    assert clean_text(text) == expected

def test_clean_text_complex_content():
    text = """
    START OF THE PROJECT GUTENBERG LICENSE\n
    Chapter 1.\n
    Hello World!  This is some content.

    With multiple   spaces.\n\n    And newlines.\n\n    End of Project Gutenberg.
    """
    expected = "Chapter 1.\n\n    Hello World!  This is some content.\n\n    With multiple   spaces.\n\n    And newlines."
    assert clean_text(text) == expected

# --- NEW TESTS FOR METADATA EXTRACTION ---

def test_extract_title_standard():
    text = """
    Title: The Great Gatsby
    Author: F. Scott Fitzgerald
    Release Date: January 1, 2023
    """
    assert extract_title(text) == "The Great Gatsby"

def test_extract_title_with_extra_whitespace():
    text = """
    Title:   Another Book Title   
    Author: Someone
    """
    assert extract_title(text) == "Another Book Title"

def test_extract_title_missing():
    text = """
    Author: Jane Doe
    Release Date: 2024
    """
    assert extract_title(text) == "Unknown Title"

def test_extract_author_standard():
    text = """
    Title: A Tale of Two Cities
    Author: Charles Dickens
    Release Date: February 2, 2023
    """
    assert extract_author(text) == "Charles Dickens"

def test_extract_author_with_extra_whitespace():
    text = """
    Title: Book
    Author:   An Author Name   
    """
    assert extract_author(text) == "An Author Name"

def test_extract_author_missing():
    text = """
    Title: A Book Without Author
    Release Date: 2024
    """
    assert extract_author(text) == "Unknown Author"


# --- CHUNKING ---

def test_sentence_breaks_are_preferred():
    paragraph = ("This is a sentence. " * 100).strip()
    pieces = split_long_paragraph(paragraph, 50)
    assert len(pieces) > 1
    assert all(piece.endswith(".") for piece in pieces)
    assert max(len(piece.split()) for piece in pieces) <= 50


def test_unpunctuated_text_falls_back_to_word_count():
    """Ulysses' last episode has no sentence-ending punctuation at all, and
    produced 4,400-word chunks before the fallback existed."""
    paragraph = " ".join(["word"] * 2000)
    pieces = split_long_paragraph(paragraph, 325)

    assert len(pieces) > 1
    assert max(len(piece.split()) for piece in pieces) <= 325
    # Nothing is lost in the split.
    assert sum(len(piece.split()) for piece in pieces) == 2000


def test_a_long_sentence_is_left_whole():
    """Only runaway sentences get cut mid-sentence; ordinary long ones do not."""
    paragraph = " ".join(["word"] * 400) + "."
    pieces = split_long_paragraph(paragraph, 325)
    assert len(pieces) == 1


def test_chunks_are_sequential_and_near_the_target():
    paragraphs = [f"Sentence number {i} goes here. " * 10 for i in range(40)]
    chunks = create_chunks(paragraphs, target_words=325)

    assert [c["sequence"] for c in chunks] == list(range(1, len(chunks) + 1))
    assert max(c["word_count"] for c in chunks) < 650
    assert all(c["content"] for c in chunks)


# --- RECAP GENERATION ---

def test_a_refused_chunk_leaves_a_gap_and_the_run_carries_on(conn, book, monkeypatch):
    """Gemini's PROHIBITED_CONTENT filter refuses some passages of classic
    literature outright. One refusal must not cost the rest of the book."""
    conn.execute("UPDATE chunks SET recap = NULL")
    seen_context = []

    def fake_recap(text, previous_recap=None):
        seen_context.append(previous_recap)
        if "part 3." in text:          # the filter refuses this one
            return None
        return f"recap for call {len(seen_context)}"

    monkeypatch.setattr(ingest.ai, "generate_recap", fake_recap)
    calls, _ = ingest.generate_recaps_for_book(conn, book)

    recaps = {row["sequence"]: row["recap"] for row in conn.execute(
        "SELECT sequence, recap FROM chunks WHERE book_id = ? ORDER BY sequence", (book,))}

    assert calls == 10                      # every chunk was attempted
    assert recaps[3] is None                # the refused one is a gap
    assert all(recaps[n] for n in (1, 2, 4, 5, 6, 7, 8, 9, 10))

    # The context carried forward across the gap rather than resetting.
    assert seen_context[3] == recaps[2]


def test_chunks_that_already_have_a_recap_cost_nothing(conn, book, monkeypatch):
    """This is also the resume path, so re-running must be cheap."""
    calls = []

    def fake_recap(text, previous_recap=None):
        calls.append(previous_recap)
        return "a new recap"

    monkeypatch.setattr(ingest.ai, "generate_recap", fake_recap)
    # The fixture leaves only the last chunk without a recap.
    made, _ = ingest.generate_recaps_for_book(conn, book)

    assert made == 1
    assert len(calls) == 1
    # It picked up the chain from the chunk before, not from nothing.
    assert calls[0] == "Recap through part 9."


def test_a_fully_recapped_book_makes_no_calls(conn, book, monkeypatch):
    conn.execute("UPDATE chunks SET recap = 'done'")
    monkeypatch.setattr(ingest.ai, "generate_recap",
                        lambda *a, **k: pytest.fail("should not call the model"))

    made, _ = ingest.generate_recaps_for_book(conn, book)
    assert made == 0
