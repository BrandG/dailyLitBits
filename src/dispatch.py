import argparse
from pymongo import MongoClient
from cryptography.fernet import Fernet
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from datetime import datetime
import pytz  # <--- NEW: Timezone handling
import config
import security

# --- CONFIGURATION ---
TARGET_HOUR = 6  # 6 AM Local Time

# Setup using Config
client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]
cipher = Fernet(config.ENCRYPTION_KEY)

def format_email_html(title, sequence, content, unsub_token, recap=None):
    # (Same as before)
    html_content = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
    base_url = "https://dailylitbits.com"
    unsub_link = f"{base_url}/unsubscribe?token={unsub_token}"

    recap_html = ""
    if recap and sequence > 1:
        recap_html = f"""
        <div style="background-color: #f8f9fa; border-left: 4px solid #6c757d; padding: 15px; margin-bottom: 25px; color: #555; font-style: italic; font-size: 14px;">
            <strong style="color: #333; font-style: normal; display: block; margin-bottom: 5px;">Previously:</strong>
            {recap}
        </div>
        """

    template = f"""
    <html>
    <body style="font-family: Georgia, 'Times New Roman', serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 10px;">
            {title}
        </h2>
        {recap_html}
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
        return response.status_code == 202
    except Exception as e:
        print(f"   [ERROR] SendGrid failed: {e}")
        return False

def send_daily_emails(debug=False, force=False):
    """
    force=True: Ignores the 6am time check (useful for testing)
    """
    print(f"--- STARTING DISPATCH RUN: {datetime.now()} (Debug={debug}, Force={force}) ---")
    
    active_subs = db.subscriptions.find({"status": "active"})
    count = 0
    
    for sub in active_subs:
        user_id = sub['user_id']
        book_id = sub['book_id']
        seq = sub['current_sequence']
        sub_id = sub['_id']
        last_sent = sub.get('last_sent')

        # 1. Fetch User to get Timezone
        user = db.users.find_one({"_id": user_id})
        if not user: 
            print(f"   [ERROR] User {user_id} not found. Skipping.")
            continue

        # --- TIMEZONE LOGIC START ---
        user_tz_str = user.get('timezone', 'UTC')
        try:
            user_tz = pytz.timezone(user_tz_str)
        except pytz.UnknownTimeZoneError:
            print(f"   [WARNING] Unknown timezone '{user_tz_str}' for user. Defaulting to UTC.")
            user_tz = pytz.utc

        # Get current time in User's location
        user_now = datetime.now(pytz.utc).astimezone(user_tz)
        
        # Check 1: Is it 6 AM? (Unless forcing)
        if not force and user_now.hour != TARGET_HOUR:
            # Silent skip so logs aren't flooded every hour
            # print(f"   [INFO] Skipping {user_id}: It is {user_now.hour}:00 in {user_tz_str} (Waiting for {TARGET_HOUR}:00)")
            continue

        # Check 2: Did we already send today?
        if last_sent:
            # Convert last_sent (UTC stored in Mongo) to User's TZ
            last_sent_local = last_sent.replace(tzinfo=pytz.utc).astimezone(user_tz)
            if last_sent_local.date() == user_now.date():
                print(f"   [INFO] Skipping {user_id}: Already sent today ({last_sent_local.strftime('%Y-%m-%d')}).")
                continue
        # --- TIMEZONE LOGIC END ---

        print(f"Processing Sub {sub_id} (It is {user_now.hour}:00 in {user_tz_str})...")

        # 2. Decrypt Email
        try:
            email = cipher.decrypt(user['email_enc']).decode()
        except Exception:
            print(f"   [ERROR] Skipping {user_id}: Decryption failed.")
            continue

        # 3. Get Content
        chunk = db.chunks.find_one({"book_id": book_id, "sequence": seq})
        if not chunk:
            print(f"   [INFO] Chunk {seq} missing. Marking {book_id} as finished for {email}!")
            db.subscriptions.update_one(
                {"_id": sub_id},
                {"$set": {"status": "completed", "completed_at": datetime.now()}}
            )
            continue

        # 4. Prepare Email
        book = db.books.find_one({"book_id": book_id})
        book_title = book['title'] if book else "Your Book"
        subject = f"{book_title}: Part {seq}"
        
        unsub_token = security.generate_unsub_token(sub_id)
        recap_text = chunk.get('recap')
        
        html_body = format_email_html(book_title, seq, chunk['content'], unsub_token, recap_text)

        # --- DEBUG MODE ---
        if debug:
            print(f"\n[DEBUG] To: {email} (Timezone: {user_tz_str})")
            print(f"[DEBUG] Subject: {subject}")
            print("-" * 40)
            print(html_body[:200] + "...") 
            print("-" * 40)
            print("[DEBUG] Stopping after first email.")
            return

        # 5. Send
        print(f"   -> Sending Part {seq} to {email}...")
        success = send_via_sendgrid(email, subject, html_body)
        
        # 6. Update DB
        if success:
            db.subscriptions.update_one(
                {"_id": sub['_id']},
                {
                    "$inc": {"current_sequence": 1},
                    "$set": {"last_sent": datetime.now()} # Saves as UTC by default
                }
            )
            count += 1
        else:
            print("   -> Failed to send.")

    if count > 0:
        print(f"\n--- RUN COMPLETE. Sent {count} emails. ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dispatch DailyLitBits emails")
    parser.add_argument("--debug", action="store_true", help="Print email without sending")
    parser.add_argument("--force", action="store_true", help="Ignore time check (send immediately)")
    args = parser.parse_args()

    send_daily_emails(debug=args.debug, force=args.force)
