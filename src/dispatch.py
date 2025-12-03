from pymongo import MongoClient
from cryptography.fernet import Fernet
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from datetime import datetime
import config
import security  # Import the new security module

# Setup using Config
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]
cipher = Fernet(config.ENCRYPTION_KEY)

def format_email_html(title, sequence, content, unsub_token):
    """
    Wraps the raw text in a simple, book-like HTML template with an unsubscribe link.
    """
    # Convert plain text newlines to HTML breaks
    html_content = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
    
    # Build Unsubscribe URL
    # In production, this should use your real domain from config or hardcoded
    base_url = "https://dailylitbits.com" 
    unsub_link = f"{base_url}/unsubscribe?token={unsub_token}"

    template = f"""
    <html>
    <body style="font-family: Georgia, 'Times New Roman', serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 10px;">
            {title}
        </h2>
        <div style="font-size: 18px;">
            <p>{html_content}</p>
        </div>
        <hr style="border: 0; border-top: 1px solid #eee; margin-top: 30px;">
        <p style="font-size: 12px; color: #999; text-align: center;">
            DailyLitBits - Part {sequence} | <a href="{unsub_link}" style="color: #999;">Unsubscribe</a>
        </p>
    </body>
    </html>
    """
    return template

def send_via_sendgrid(to_email, subject, html_body):
    message = Mail(
        from_email=config.FROM_EMAIL,
        to_emails=to_email,
        subject=subject,
        html_content=html_body)
    
    try:
        sg = SendGridAPIClient(config.SENDGRID_API_KEY)
        response = sg.send(message)
        print(f"   [API] Status Code: {response.status_code}")
        return response.status_code == 202
    except Exception as e:
        print(f"   [ERROR] SendGrid failed: {e}")
        return False

def send_daily_emails():
    print(f"--- STARTING DISPATCH RUN: {datetime.now()} ---")
    
    active_subs = db.subscriptions.find({"status": "active"})
    count = 0
    
    for sub in active_subs:
        user_id = sub['user_id']
        book_id = sub['book_id']
        seq = sub['current_sequence']
        sub_id = sub['_id'] # Needed for unsubscribe token
        
        # 1. Decrypt Email
        user = db.users.find_one({"_id": user_id})
        if not user: continue
            
        try:
            email = cipher.decrypt(user['email_enc']).decode()
        except Exception:
            print(f"Skipping {user_id}: Decryption failed.")
            continue

        # 2. Get Content
        chunk = db.chunks.find_one({"book_id": book_id, "sequence": seq})
        if not chunk:
            print(f"User {email} finished {book_id}!")
            # Logic to mark complete goes here
            # Mark subscription as completed
            db.subscriptions.update_one(
                {"_id": sub_id},
                {"$set": {"status": "completed", "completed_at": datetime.now()}}
            )
            continue

        # 3. Prepare Email
        book = db.books.find_one({"book_id": book_id})
        book_title = book['title'] if book else "Your Book"
        subject = f"{book_title}: Part {seq}"
        
        # Generate the Unsubscribe Token
        unsub_token = security.generate_unsub_token(sub_id)
        
        html_body = format_email_html(book_title, seq, chunk['content'], unsub_token)

        # 4. Send
        print(f"-> Sending Part {seq} of {book_title} to {email}...")
        success = send_via_sendgrid(email, subject, html_body)
        
        # 5. Update DB
        if success:
            db.subscriptions.update_one(
                {"_id": sub['_id']},
                {
                    "$inc": {"current_sequence": 1},
                    "$set": {"last_sent": datetime.now()}
                }
            )
            count += 1
        else:
            print("   -> Failed to send. Will retry next run.")

    print(f"\n--- RUN COMPLETE. Sent {count} emails. ---")

if __name__ == "__main__":
    send_daily_emails()
