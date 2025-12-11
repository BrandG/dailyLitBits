import argparse
from pymongo import MongoClient
from cryptography.fernet import Fernet
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from datetime import datetime, timedelta
import pytz
import config
import security
from bson import ObjectId
import random
import ai

# --- CONFIGURATION ---
TARGET_HOUR = 6
BINGE_COOLDOWN_MINUTES = 5 

client = MongoClient(config.MONGO_URI)
db = client[config.DB_NAME]
cipher = Fernet(config.ENCRYPTION_KEY)

def format_email_html(title, sequence, total_chunks, content, unsub_token, binge_token, recap=None):
    html_content = content.replace('\n\n', '</p><p>').replace('\n', '<br>')
    base_url = "https://dailylitbits.com"
    unsub_link = f"{base_url}/unsubscribe?token={unsub_token}"
    binge_link = f"{base_url}/next?token={binge_token}"
    dashboard_link = f"{base_url}/profile?token={binge_token}"

    if total_chunks < 1: total_chunks = 1
    percent = int((sequence / total_chunks) * 100)
    if percent < 1: percent = 1
    
    # Calculate widths for the table cells
    green_width = percent
    gray_width = 100 - percent

    recap_html = ""
    if recap and sequence > 1:
        recap_html = f"""
        <div style="background-color: #f8f9fa; border-left: 4px solid #6c757d; padding: 15px; margin-bottom: 25px; color: #555; font-style: italic; font-size: 14px;">
            <strong style="color: #333; font-style: normal; display: block; margin-bottom: 5px;">Previously:</strong>
            {recap}
        </div>
        """

    binge_button_html = f"""
    <div style="text-align: center; margin-top: 30px;">
        <a href="{binge_link}" style="background-color: #2c3e50; color: #ffffff; padding: 10px 20px; text-decoration: none; border-radius: 5px; font-size: 14px; font-weight: bold;">
            Send Part {sequence + 1} Now
        </a>
        <p style="font-size: 11px; color: #999; margin-top: 5px;">(Or wait until tomorrow 6 AM)</p>
    </div>
    """

    template = f"""
    <html>
    <body style="font-family: Georgia, 'Times New Roman', serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        
        <div style="border-bottom: 2px solid #eee; padding-bottom: 15px; margin-bottom: 20px;">
            <h2 style="color: #2c3e50; margin: 0 0 10px 0;">{title}</h2>
            
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-top: 10px;">
                <tr>
                    <td width="85%" style="vertical-align: middle;">
                        <table width="100%" cellpadding="0" cellspacing="0" height="8" style="height: 8px; background-color: #eeeeee; border-radius: 4px; overflow: hidden;">
                            <tr>
                                <td width="{green_width}%" height="8" style="background-color: #28a745; height: 8px; line-height: 0; font-size: 0;">&nbsp;</td>
                                <td width="{gray_width}%" height="8" style="background-color: #eeeeee; height: 8px; line-height: 0; font-size: 0;">&nbsp;</td>
                            </tr>
                        </table>
                    </td>
                    <td width="15%" style="text-align: right; font-family: sans-serif; font-size: 12px; color: #999; white-space: nowrap; padding-left: 10px;">
                        Part {sequence} of {total_chunks}
                    </td>
                </tr>
            </table>
        </div>

        {recap_html}
        
        <div style="font-size: 18px;">
            <p>{html_content}</p>
        </div>
        
        {binge_button_html}
        
        <hr style="border: 0; border-top: 1px solid #eee; margin-top: 30px;">
        
        <p style="font-size: 12px; color: #999; text-align: center; font-family: sans-serif;">
            <a href="{dashboard_link}" style="color: #2c3e50; font-weight: bold; text-decoration: none;">Manage Subscription</a> 
            &nbsp;|&nbsp; 
            <a href="{unsub_link}" style="color: #999;">Unsubscribe</a>
        </p>
    </body>
    </html>
    """
    return template

# --- UPGRADED VICTORY TEMPLATE (Table-Based for Email Compatibility) ---
def format_victory_email(book_title, days_taken, word_count, suggestions, switch_token, additional_message=None):
    base_url = "https://dailylitbits.com"

    # Grammar fix: "1 Day" vs "2 Days"
    day_label = "Day" if days_taken == 1 else "Days"

    # Create HTML for the 3 suggested books
    suggestions_html = ""
    for book in suggestions:
        link = f"{base_url}/switch_book?token={switch_token}&new_book_id={book['book_id']}"
        author = book.get('author', 'Unknown Author')

        suggestions_html += f"""
        <div style="background: #fff; border: 1px solid #ddd; padding: 15px; margin-bottom: 15px; border-radius: 5px; text-align: left;">
            <strong style="font-size: 16px; color: #2c3e50;">{book['title']}</strong>
            <div style="font-size: 12px; color: #777; margin-bottom: 8px;">by {author}</div>
            <div>
                <a href="{link}" style="color: #28a745; text-decoration: none; font-weight: bold; font-size: 14px;">
                    Start Reading &#8594;
                </a>
            </div>
        </div>
        """

    # Render additional message if provided
    message_html = ""
    if additional_message:
        message_html = f"<p style=\"margin-top: 20px; font-size: 16px;\">{additional_message}</p>"

    template = f"""
    <html>
    <body style="font-family: sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; text-align: center;">
        <h1 style="color: #28a745; margin-bottom: 10px;">Congratulations!</h1>
        <h2 style="color: #555; font-weight: normal; margin-top: 0;">You have finished<br><strong>{book_title}</strong></h2>

        {message_html}

        <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f8f9fa; border: 1px solid #ddd; border-radius: 10px; margin: 30px 0; border-collapse: separate;">
            <tr>
                <td width="50%" style="padding: 20px; text-align: center; border-right: 1px solid #ddd; vertical-align: middle;">
                    <p style="font-size: 12px; margin: 0; color: #999; text-transform: uppercase; letter-spacing: 1px;">Time Taken</p>
                    <p style="font-size: 28px; font-weight: bold; color: #2c3e50; margin: 5px 0 0 0;">{days_taken} {day_label}</p>
                </td>
                <td width="50%" style="padding: 20px; text-align: center; vertical-align: middle;">
                    <p style="font-size: 12px; margin: 0; color: #999; text-transform: uppercase; letter-spacing: 1px;">Words Read</p>
                    <p style="font-size: 28px; font-weight: bold; color: #2c3e50; margin: 5px 0 0 0;">{word_count:,}</p>
                </td>
            </tr>
        </table>

        <h3 style="color: #2c3e50; margin-top: 40px; border-bottom: 2px solid #eee; padding-bottom: 10px;">Ready for your next adventure?</h3>
        <p style="color: #666; font-size: 14px; margin-bottom: 20px;">Click a book below to start receiving it tomorrow.</p>

        <div style="background-color: #f4f4f4; padding: 20px; border-radius: 8px;">
            {suggestions_html}
        </div>

        <p style="margin-top: 30px; font-size: 12px; color: #999;">
            <a href="{base_url}" style="color: #999;">Browse Full Library</a>
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

def process_subscription(sub_id, trigger="cron", debug=False):
    sub = db.subscriptions.find_one({"_id": ObjectId(sub_id)})
    if not sub: return False, "Subscription not found"
    
    # We allow 'completed' status here specifically to let them re-trigger the victory email if needed
    if sub['status'] != 'active' and sub['status'] != 'completed': 
        return False, "Subscription is not active"

    # Rate Limit Check (Binge Only)
    if trigger == 'binge':
        last_sent = sub.get('last_sent')
        if last_sent:
            if last_sent.tzinfo is None: last_sent = pytz.utc.localize(last_sent)
            now_utc = datetime.now(pytz.utc)
            diff = now_utc - last_sent
            if diff < timedelta(minutes=BINGE_COOLDOWN_MINUTES):
                wait_time = BINGE_COOLDOWN_MINUTES - int(diff.total_seconds() / 60)
                return False, f"Please wait {wait_time} minutes before requesting the next chapter."

    user_id = sub['user_id']
    book_id = sub['book_id']
    seq = sub['current_sequence']
    
    user = db.users.find_one({"_id": user_id})
    try:
        email = cipher.decrypt(user['email_enc']).decode()
    except Exception:
        return False, "Decryption failed"

    chunk = db.chunks.find_one({"book_id": book_id, "sequence": seq})
    
    # --- VICTORY LOGIC ---
    if not chunk:
        # Calculate stats
        start_date = sub.get('created_at', datetime.now())
        if start_date.tzinfo is None: start_date = pytz.utc.localize(start_date)
        now_utc = datetime.now(pytz.utc)
        days_taken = (now_utc - start_date).days
        if days_taken < 1: days_taken = 1
        
        # Calculate Word Count (Sum of all chunks)
        pipeline = [
            {"$match": {"book_id": book_id}},
            {"$group": {"_id": None, "total_words": {"$sum": "$word_count"}}}
        ]
        result = list(db.chunks.aggregate(pipeline))
        total_words = result[0]['total_words'] if result else 0

        print(f"    [Victory] Generating smart recommendations for {user_id}...")
        
        # B. Get Titles of Read Books (for the AI Prompt)
        # We just need a list of titles like ["Dracula", "Frankenstein"]
        read_titles = db.books.distinct("title", {"parent_id": {"$in": list(read_parent_ids)}})

        # C. Fetch Available Library (Only 'standard' edition candidates, excluding read ones)
        # We filter by parent_id not being in our read list
        available_cursor = db.books.find(
            {
                "parent_id": {"$nin": list(read_parent_ids)},
                "chunk_size": 750 # Only recommend Standard editions to keep list clean
            },
            {"book_id": 1, "title": 1, "author": 1}
        )

        library_catalog = []
        for b in available_cursor:
            library_catalog.append({
                "id": b['book_id'],
                "title": b['title'],
                "author": b.get('author', 'Unknown')
            })

        # D. Get AI Picks
        suggestions = []
        if library_catalog:
            # Only ask AI if we have books to suggest
            recommended_ids = ai.get_recommendations(read_titles, library_catalog)

            if recommended_ids:
                # Fetch the full book objects for the IDs returned by AI
                suggestions = list(db.books.find({"book_id": {"$in": recommended_ids}}))

        # E. Fallback (If AI fails or returns nothing, use random)
        if not suggestions:
            print("   [Victory] AI failed or no result. Falling back to random.")
            suggestions = list(db.books.aggregate([
                {"$match": {
                    "parent_id": {"$nin": list(read_parent_ids)},
                    "chunk_size": 750
                }},
                {"$sample": {"size": 3}}
            ]))
        
        # --- ACTIVATE NEXT QUEUED BOOK ---
        next_queued_sub = db.subscriptions.find_one(
            {"user_id": user_id, "status": "queued"},
            sort=[("created_at", 1)] # Get the oldest one
        )

        next_book_title = None
        if next_queued_sub:
            print(f"   [Victory] Activating next queued book for {user_id}: {next_queued_sub['book_id']}")
            db.subscriptions.update_one(
                {"_id": next_queued_sub['_id']},
                {"$set": {
                    "status": "active", 
                    "current_sequence": 1, 
                    "created_at": datetime.now(), # Reset stats for the new book
                    "last_sent": None # Allows immediate sending of the first part
                }}
            )
            # Fetch title for email notification
            next_book_info = db.books.find_one({"book_id": next_queued_sub['book_id']})
            if next_book_info:
                next_book_title = next_book_info['title']
        # --------------------------------

        # F. Send Victory Email
        switch_token = security.generate_binge_token(sub_id)
        book_title = curr_book['title'] if curr_book else "Your Book"
        subject = f"You finished {book_title}!"

        # --- MODIFY VICTORY EMAIL CONTENT ---        
        victory_message = f"Congratulations! You have finished <strong>{book_title}</strong>."
        if next_book_title:
            victory_message += f"<p style=\"margin-top: 20px;\">Your next book, <strong>{next_book_title}</strong>, will start arriving tomorrow!</p>"
        
        html_body = format_victory_email(book_title, days_taken, total_words, suggestions, switch_token, additional_message=victory_message)
        # ------------------------------------
