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

def run_cron():
    """Processes all active subscriptions for the cron job."""
    print("Running dispatch.py in cron mode...")
    # Find all active subscriptions
    subscriptions = list(db.subscriptions.find({'status': 'active'}))
    if not subscriptions:
        print("   No active subscriptions found.")
        return

    for sub in subscriptions:
        sub_id = str(sub['_id'])
        success, message = process_subscription(sub_id, trigger='cron')
        if success:
            print(f"   Successfully dispatched for subscription {sub_id}")
        else:
            print(f"   Failed to dispatch for subscription {sub_id}: {message}")

def run_force(sub_id):
    """Processes a specific subscription by ID."""
    print(f"Running dispatch.py in force mode for subscription ID: {sub_id}...")
    success, message = process_subscription(sub_id, trigger='binge')
    if success:
        print(f"   Successfully dispatched for subscription {sub_id}")
    else:
        print(f"   Failed to dispatch for subscription {sub_id}: {message}")

# --- CONFIGURATION ---
TARGET_HOUR = 6
BINGE_COOLDOWN_MINUTES = 5 

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
    # NOTE: 'db' and 'cipher' are now initialized in __main__ and made available globally if needed,
    # but ideally, they should be passed as arguments or accessed via a shared context.
    # For now, assuming they are accessible globally after __main__ initialization.
    sub = db.subscriptions.find_one({"_id": ObjectId(sub_id)})
    if not sub: return False, "Subscription not found"
    
    # We allow 'completed' status here specifically to let them re-trigger the victory email if needed
    if sub['status'] != 'active' and sub['status'] != 'completed': 
        return False, "Subscription is not active"

    user_id = sub['user_id']
    user = db.users.find_one({"_id": user_id})
    if not user: return False, "User not found"

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

    # Delivery Time Check (Cron Only)
    if trigger == 'cron':
        user_tz_str = user.get('timezone', 'UTC')
        try:
            user_tz = pytz.timezone(user_tz_str)
        except pytz.UnknownTimeZoneError:
            user_tz = pytz.UTC
            
        now_user_tz = datetime.now(user_tz)
        target_hour = sub.get('delivery_hour', TARGET_HOUR) # Default to 6
        
        # 1. Check Hour
        if now_user_tz.hour != target_hour:
             return False, f"Not delivery time yet. (Current: {now_user_tz.hour}, Target: {target_hour})"

        # 2. Check Frequency (Already sent today?)
        last_sent = sub.get('last_sent')
        if last_sent:
            if last_sent.tzinfo is None:
                last_sent = pytz.utc.localize(last_sent)
            
            last_sent_user_tz = last_sent.astimezone(user_tz)
            
            if last_sent_user_tz.date() == now_user_tz.date():
                return False, "Already sent today."

    book_id = sub['book_id']
    seq = sub['current_sequence']
    
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

        # A. Determine which books the user has already read (by parent_id)
        # Find all completed subscriptions for the user
        completed_subs = db.subscriptions.find({"user_id": user_id, "status": "completed"})
        completed_book_ids = [sub['book_id'] for sub in completed_subs]

        # Get the parent_ids for all completed books
        read_parent_ids_cursor = db.books.find({"book_id": {"$in": completed_book_ids}}, {"parent_id": 1})
        read_parent_ids = {b['parent_id'] for b in read_parent_ids_cursor if 'parent_id' in b}

        # Also add the parent_id of the book just finished
        current_book_info = db.books.find_one({"book_id": book_id})
        if current_book_info and 'parent_id' in current_book_info:
            read_parent_ids.add(current_book_info['parent_id'])
        
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
        # 'curr_book' is still not defined here, this will cause a NameError.
        # I will assume 'curr_book' should be fetched for the current subscription.
        current_book_info = db.books.find_one({"book_id": book_id})
        book_title = current_book_info['title'] if current_book_info else "Your Book" 
        subject = f"You finished {book_title}!"

        # --- MODIFY VICTORY EMAIL CONTENT ---        
        victory_message = f"Congratulations! You have finished <strong>{book_title}</strong>."
        if next_book_title:
            victory_message += f"<p style=\"margin-top: 20px;\">Your next book, <strong>{next_book_title}</strong>, will start arriving tomorrow!</p>"
        
        html_body = format_victory_email(book_title, days_taken, total_words, suggestions, switch_token, additional_message=victory_message)
        if send_via_sendgrid(email, subject, html_body):
            db.subscriptions.update_one(
                {"_id": ObjectId(sub_id)},
                {"$set": {"status": "completed", "last_sent": datetime.now(pytz.utc)}}
            )
            return True, "Victory email sent."
        else:
            return False, "Failed to send victory email."
    
    # --- REGULAR DISPATCH LOGIC ---
    if not chunk:
        # This case should ideally not be reached if victory logic is correct,
        # but as a safeguard, we mark subscription complete if no chunk found
        db.subscriptions.update_one(
            {"_id": ObjectId(sub_id)},
            {"$set": {"status": "completed"}}
        )
        return False, "No chunk found for current sequence. Subscription marked completed."

    # Fetch total chunks for progress display
    total_chunks = db.chunks.count_documents({"book_id": book_id})

    # Generate tokens for unsubscribe and binge reading
    unsub_token = security.generate_unsub_token(email)
    binge_token = security.generate_binge_token(sub_id)

    # Get recap if not the first chunk
    recap_text = None
    if seq > 1:
        prev_chunk = db.chunks.find_one({"book_id": book_id, "sequence": seq - 1})
        if prev_chunk:
            print(f"    [AI] Generating recap for chunk {seq} of {book_id}...")
            recap_text = ai.generate_recap(prev_chunk['content'])

    # Format and send email
    current_book_info = db.books.find_one({"book_id": book_id})
    book_title = current_book_info['title'] if current_book_info else "dailyLitBits"
    subject = f"dailyLitBits: {book_title} (Part {seq}/{total_chunks})"
    
    html_body = format_email_html(
        book_title, seq, total_chunks, chunk['content'], unsub_token, binge_token, recap_text
    )

    if send_via_sendgrid(email, subject, html_body):
        # Update subscription for next dispatch
        db.subscriptions.update_one(
            {"_id": ObjectId(sub_id)},
            {
                "$inc": {"current_sequence": 1},
                "$set": {"last_sent": datetime.now(pytz.utc)}
            }
        )
        return True, "Email dispatched successfully."
    else:
        return False, "Failed to send email."


# --- Database and Cipher Initialization Functions ---
def initialize_db():
    """Initializes and returns the MongoDB client and db object."""
    try:
        client = MongoClient(config.MONGO_URI)
        # The ismaster command is cheap and does not require auth.
        client.admin.command('ismaster') 
        db = client[config.DB_NAME]
        print("MongoDB connection established successfully.")
        return client, db
    except Exception as e:
        print(f"   [ERROR] Failed to connect to MongoDB: {e}")
        # Raise the exception to stop execution if DB connection fails
        raise

def initialize_cipher():
    """Initializes and returns the Fernet cipher."""
    try:
        cipher = Fernet(config.ENCRYPTION_KEY)
        print("Fernet cipher initialized successfully.")
        return cipher
    except Exception as e:
        print(f"   [ERROR] Failed to initialize Fernet cipher: {e}")
        # Raise the exception to stop execution if cipher fails
        raise

if __name__ == "__main__":
    # Initialize DB and Cipher here
    try:
        # These are now available globally within this script's execution context
        client, db = initialize_db() 
        cipher = initialize_cipher()
    except Exception as e:
        # If initialization fails, exit.
        print(f"Critical initialization failed: {e}")
        exit(1) # Exit with an error code

    parser = argparse.ArgumentParser(description="Dispatch emails for dailyLitBits.")
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Subparser for the 'cron' command (default behavior)
    parser_cron = subparsers.add_parser('cron', help='Run dispatch for all active subscriptions (default).')
    parser_cron.set_defaults(func=run_cron)

    # Subparser for the 'force' command
    parser_force = subparsers.add_parser('force', help='Force dispatch for a specific subscription ID.')
    parser_force.add_argument('sub_id', type=str, help='The ID of the subscription to force dispatch for.')
    parser_force.set_defaults(func=lambda args: run_force(args.sub_id))

    args = parser.parse_args()

    # If no command is specified, default to 'cron'
    if hasattr(args, 'command') and args.command is None:
        run_cron()
    elif hasattr(args, 'func'):
        args.func(args)
    else:
        # Fallback if no command is recognized or if help is implicitly shown
        parser.print_help()
