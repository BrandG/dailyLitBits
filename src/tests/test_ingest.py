import pytest
import os
import sys

# --- PATH SETUP ---
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
import config

from ingest import clean_text, extract_title, extract_author

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