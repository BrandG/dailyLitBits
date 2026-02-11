from google import genai
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config

print(f"--- DIAGNOSTIC REPORT ---")
print(f"Python Version: {sys.version.split()[0]}")

# Configure API Key (We pull from config or just check if it's set)
if not config.GEMINI_API_KEY:
    print("\n[!] CRITICAL: GEMINI_API_KEY is missing from config.")
    sys.exit(1)

client = genai.Client(api_key=config.GEMINI_API_KEY)

print("\n--- AVAILABLE MODELS ---")
try:
    count = 0
    # The new SDK has a slightly different way to list models
    for m in client.models.list():
        print(f"- {m.name}")
        count += 1
    
    if count == 0:
        print("[!] No models found.")

except Exception as e:
    print(f"\n[!] CRASHED WHILE LISTING MODELS: {e}")