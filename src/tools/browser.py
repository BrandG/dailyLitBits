import argparse
import requests
import re
# from bs4 import BeautifulSoup <-- Deleted as requested

BASE_URL = "http://localhost:8000"

def clean_html(html_content):
    """
    Strips HTML tags to show readable text. 
    """
    text = re.sub(r'<style.*?</style>', '', html_content, flags=re.DOTALL) 
    text = re.sub(r'<script.*?</script>', '', text, flags=re.DOTALL) 
    text = re.sub(r'<[^>]+>', '\n', text) 
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)

def make_request(method, endpoint, data=None, raw=False):
    url = f"{BASE_URL}{endpoint}"
    
    # Only print metadata if NOT in raw mode (so we don't pollute the HTML file)
    if not raw:
        print(f"\n--- {method} {url} ---")
    
    try:
        if method == "GET":
            resp = requests.get(url)
        else:
            resp = requests.post(url, data=data)
            
        if not raw:
            print(f"STATUS: {resp.status_code}")
            if resp.history:
                print("Redirected from:", resp.history[0].url)
            print("\n--- RESPONSE CONTENT ---")
            print(clean_html(resp.text))
            print("------------------------\n")
        else:
            # RAW MODE: Just print the HTML and nothing else
            print(resp.text)
            
    except Exception as e:
        if not raw:
            print(f"ERROR: {e}")
        else:
            print(f"")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DailyLitBits CLI Browser")
    subparsers = parser.add_subparsers(dest="command")

    # GET Command
    get_parser = subparsers.add_parser("get", help="Perform GET request")
    get_parser.add_argument("endpoint", help="/path?query=...")
    get_parser.add_argument("--raw", action="store_true", help="Output raw HTML only")

    # POST Command
    post_parser = subparsers.add_parser("post", help="Perform POST request")
    post_parser.add_argument("endpoint", help="/path")
    post_parser.add_argument("--data", "-d", nargs="+", help="key=value key2=value2 ...")
    post_parser.add_argument("--raw", action="store_true", help="Output raw HTML only")

    args = parser.parse_args()

    if args.command == "get":
        make_request("GET", args.endpoint, raw=args.raw)
        
    elif args.command == "post":
        form_data = {}
        if args.data:
            for item in args.data:
                k, v = item.split("=", 1)
                form_data[k] = v
        
        make_request("POST", args.endpoint, form_data, raw=args.raw)
    
    else:
        parser.print_help()
