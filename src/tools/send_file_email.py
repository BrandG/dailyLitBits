import argparse
import sys
import os

# Add the 'src' directory to the Python path to allow importing 'dispatch'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatch import send_via_sendgrid

def send_file_by_email(file_path):
    """
    Reads the content of a file and emails it.
    """
    recipient_email = "brandg@gmail.com"
    subject = f"File Content: {os.path.basename(file_path)}"

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        print(f"Sending content of '{file_path}' to {recipient_email}...")
        
        success = send_via_sendgrid(recipient_email, subject, html_content)
        
        if success:
            print("Email sent successfully!")
        else:
            print("Failed to send email.")
            
    except FileNotFoundError:
        print(f"Error: The file '{file_path}' was not found.")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a file's content via email.")
    parser.add_argument("file_path", help="The path to the file to send.")
    args = parser.parse_args()

    send_file_by_email(args.file_path)
