import google.generativeai as genai
from google.generativeai import types
import config
import time
import random
from google.api_core import exceptions
import json
import re

GENAI_MODEL_NAME = 'gemini-flash-latest' 
# Setup
if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)

SAFETY_SETTINGS = {
    types.HarmCategory.HARM_CATEGORY_HARASSMENT: types.HarmBlockThreshold.BLOCK_NONE,
    types.HarmCategory.HARM_CATEGORY_HATE_SPEECH: types.HarmBlockThreshold.BLOCK_NONE,
    types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: types.HarmBlockThreshold.BLOCK_NONE,
    types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: types.HarmBlockThreshold.BLOCK_NONE,
    #types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY: types.HarmBlockThreshold.BLOCK_NONE,
}

def generate_recap(current_text_chunk, previous_recap=None):
    if not config.GEMINI_API_KEY:
        print("   [AI Error] No GEMINI_API_KEY found.")
        return None

    model = genai.GenerativeModel(GENAI_MODEL_NAME)
    
    # Prepare Prompt
    if not previous_recap:
        prompt = f"""
        You are a literary assistant analyzing a classic public domain novel.
        Summarize the following opening book excerpt in 2-3 sentences.
        Focus on identifying the main characters and the setting. Use specific names.
        
        CONTEXT: This is a fictional story (Public Domain). Do not censor literary themes.
        
        TEXT:
        {current_text_chunk[:10000]} 
        """
    else:
        prompt = f"""
        You are writing a 'Previously On' recap for a serialized novel.
        
        CONTEXT: This is a fictional story (Public Domain).
        
        STORY CONTEXT (What happened before):
        {previous_recap}
        
        NEW TEXT (Just happened):
        {current_text_chunk[:10000]}
        
        TASK:
        Write a concise (2-3 sentences) summary of the NEW TEXT that integrates it with the STORY CONTEXT.
        - Explicitly name characters (e.g. use "Gregor", not "he").
        - Explain how the plot has advanced.
        - Start with "Previously:" or just the summary.
        """

    # --- NEW: RETRY LOGIC ---
    max_retries = 5
    base_delay = 5 # seconds

    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt,safety_settings=SAFETY_SETTINGS, request_options={'timeout': 60})
            if not response.parts:
                raise ValueError("Model returned empty response (Ghost Output)")

            return response.text.strip()
            
        except exceptions.ResourceExhausted as e:
            # 1. ADD JITTER: Wait base_delay + random(0-3s)
            jitter = random.uniform(0, 3)
            wait_time = (base_delay * (2 ** attempt)) + jitter
            # 2. PRINT REAL ERROR: We need to see if it mentions "Day" or "Minute"
            print(f"   [429 Hit] {e}") 
            print(f"   -> Cooling down for {wait_time:.2f}s...")
            time.sleep(wait_time)
            
        except Exception as e:
            # 504s and "Invalid operation" errors will be caught here and retried
            print(f"   [AI Error] Attempt {attempt+1} failed: {e}")
            time.sleep(2) # Short pause before retry 
    
    print("   [AI Error] Max retries exceeded.")
    return None

def get_recommendations(read_titles, available_books):
    """
    Asks Gemini to pick 3 books from 'available_books' based on 'read_titles'.
    Returns a list of 3 book_ids.
    """
    if not config.GEMINI_API_KEY:
        return []

    model = genai.GenerativeModel(GENAI_MODEL_NAME)

    # Convert available books to a lightweight text list for the prompt
    # Format: "ID: Title by Author"
    library_text = ""
    for b in available_books:
        library_text += f"{b['id']}: {b['title']} by {b['author']}\n"

    prompt = f"""
    You are a librarian.

    THE USER HAS READ:
    {", ".join(read_titles)}

    THE AVAILABLE LIBRARY:
    {library_text}

    TASK:
    Select exactly 3 books from the LIBRARY that the user would enjoy based on what they have read.
    Provide a brief reason for each (but we only need the IDs).

    OUTPUT FORMAT:
    Return ONLY a raw JSON list of the book IDs. Do not use markdown blocks.
    Example: ["pg123", "pg99", "pg45"]
    """

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()

        # Cleanup: Remove markdown code blocks if Gemini adds them
        if text.startswith("```"):
            text = re.sub(r"^```json|^```", "", text).strip()
            text = re.sub(r"```$", "", text).strip()

        return json.loads(text)

    except Exception as e:
        print(f"   [AI Recommendation Error] {e}")
        return []
