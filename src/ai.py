import google.generativeai as genai
import config

# Setup
if config.GEMINI_API_KEY:
    genai.configure(api_key=config.GEMINI_API_KEY)

def generate_recap(current_text_chunk, previous_recap=None):
    """
    Generates a rolling summary.
    Input:
      - current_text_chunk: The text of the chapter we just finished.
      - previous_recap: The summary of the story UP TO this chapter.
    """
    if not config.GEMINI_API_KEY:
        print("   [AI Error] No GEMINI_API_KEY found.")
        return None

    model = genai.GenerativeModel('gemini-2.0-flash')
    
    # CASE 1: First Chapter (No previous context)
    if not previous_recap:
        prompt = f"""
        You are a literary assistant. Summarize the following opening book excerpt in 2-3 sentences.
        Focus on identifying the main characters and the setting. Use specific names.
        
        TEXT:
        {current_text_chunk[:10000]} 
        """
    
    # CASE 2: Rolling Context (We have history)
    else:
        prompt = f"""
        You are writing a 'Previously On' recap for a serialized novel.
        
        STORY CONTEXT (What happened before):
        {previous_recap}
        
        NEW TEXT (Just happened):
        {current_text_chunk[:10000]}
        
        TASK:
        Write a concise (2-3 sentences) summary of the NEW TEXT that integrates it with the STORY CONTEXT.
        - explicitely name characters (e.g. use "Peter Pan", not "he").
        - Explain how the plot has advanced.
        - Start with "Previously:" or just the summary.
        """
    
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"   [AI Error] Gemini call failed: {e}")
        return None