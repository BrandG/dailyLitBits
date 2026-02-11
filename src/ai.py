from google import genai
from google.genai import types
import config
import time
import random
import json
import re

# Use the recommended model name for the new SDK
GENAI_MODEL_NAME = 'gemini-2.0-flash' 

# Initialize client if API key is present
client = None
if config.GEMINI_API_KEY:
    client = genai.Client(api_key=config.GEMINI_API_KEY)

# Define safety settings for the new SDK
SAFETY_SETTINGS = [
    types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_NONE'),
    types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
    types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_NONE'),
    types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
]

def generate_recap(current_text_chunk, previous_recap=None):
    if not client:
        print("   [AI Error] No GEMINI_API_KEY found or client not initialized.")
        return None

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

    # --- RETRY LOGIC ---
    max_retries = 5
    base_delay = 5 # seconds

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=GENAI_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    safety_settings=SAFETY_SETTINGS,
                    # timeout is set differently in the new SDK or managed by the client
                )
            )
            
            if not response.text:
                raise ValueError("Model returned empty response")

            return response.text.strip()
            
        except Exception as e:
            # Check for rate limiting or other retriable errors
            error_str = str(e).lower()
            if "429" in error_str or "resource_exhausted" in error_str:
                jitter = random.uniform(0, 3)
                wait_time = (base_delay * (2 ** attempt)) + jitter
                print(f"   [429 Hit] {e}") 
                print(f"   -> Cooling down for {wait_time:.2f}s...")
                time.sleep(wait_time)
            else:
                print(f"   [AI Error] Attempt {attempt+1} failed: {e}")
                time.sleep(2) 
    
    print("   [AI Error] Max retries exceeded.")
    return None

def get_recommendations(read_titles, available_books):
    """
    Asks Gemini to pick 3 books from 'available_books' based on 'read_titles'.
    Returns a list of 3 book_ids.
    """
    if not client:
        return []

    # Convert available books to a lightweight text list for the prompt
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
        response = client.models.generate_content(
            model=GENAI_MODEL_NAME,
            contents=prompt
        )
        text = response.text.strip()

        # Cleanup: Remove markdown code blocks if Gemini adds them
        if text.startswith("```"):
            text = re.sub(r"^```json|^```", "", text).strip()
            text = re.sub(r"```$", "", text).strip()

        return json.loads(text)

    except Exception as e:
        print(f"   [AI Recommendation Error] {e}")
        return []

