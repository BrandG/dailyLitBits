import google.generativeai as genai
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config

print(f"--- DIAGNOSTIC REPORT ---")
print(f"Python Version: {sys.version.split()[0]}")
try:
    print(f"Generative AI Library Version: {genai.__version__}")
except AttributeError:
    print("Generative AI Library Version: <Unknown / Very Old>")

# Configure API Key (We pull from config or just check if it's set)
if not config.GEMINI_API_KEY:
    print("\n[!] CRITICAL: GEMINI_API_KEY is missing from config.")
    sys.exit(1)

genai.configure(api_key=config.GEMINI_API_KEY)

print("\n--- AVAILABLE MODELS ---")
try:
    count = 0
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"- {m.name}")
            count += 1
    
    if count == 0:
        print("[!] No models found that support 'generateContent'.")
        print("    This usually means the API Key is invalid or has no access.")

except Exception as e:
    print(f"\n[!] CRASHED WHILE LISTING MODELS: {e}")