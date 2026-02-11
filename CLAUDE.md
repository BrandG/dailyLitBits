# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

dailyLitBits is a FastAPI-based web application that delivers serialized classic literature excerpts to subscribers via email. Users subscribe to books, receive daily parts (chunks), and can manage their reading through a token-based dashboard. The application uses MongoDB for data storage, Google Gemini AI for content generation, and Brevo for email delivery.

## Development Environment

All development and operations **MUST** be run inside the Docker container. Never run Python commands directly on the host.

### Build and Run Commands

```bash
# Build/rebuild the Docker image (required after changing requirements.txt)
docker compose -f docker/docker-compose.yml build

# Start services
docker compose -f docker/docker-compose.yml up -d

# Restart web service (required after changing Python code)
docker compose -f docker/docker-compose.yml restart web

# Stop services
docker compose -f docker/docker-compose.yml down
```

### Running Scripts

All scripts must be run using `docker exec`. The container's working directory is `/app`, which maps to the host's `src/` directory.

```bash
# General syntax
docker exec -it dailylitbits python <script_name>.py [args]

# Examples
docker exec -it dailylitbits python ingest.py pg11
docker exec -it dailylitbits python dispatch.py force <subscription_id>
docker exec -it dailylitbits python tools/audit_library.py
```

### Testing

Tests are run using pytest from within the Docker container. The test suite uses a separate temporary database for isolation.

```bash
# Run all tests
docker exec -it dailylitbits pytest tests/

# Run specific test file
docker exec -it dailylitbits pytest tests/test_main.py

# Run with verbose output
docker exec -it dailylitbits pytest tests/ -v
```

## Core Architecture

### Application Entry Point
- **`src/main.py`**: FastAPI application with all HTTP endpoints
  - Uses dependency injection pattern: `Depends(get_db)` for database access
  - Token-based authentication (no session cookies)
  - All routes that need database access use the `get_db` dependency

### Database Architecture
- **MongoDB Collections**:
  - `books`: Book metadata (title, author, total_chunks, edition, parent_id)
  - `chunks`: Individual text chunks (book_id, sequence, content, word_count)
  - `users`: User accounts with encrypted emails (email_enc, username, password_hash, is_claimed)
  - `subscriptions`: Active/queued/completed/paused reading assignments (user_id, book_id, current_sequence, status)
  - `suggestions`: User-submitted book suggestions

### Multi-Edition System
Books are stored in three editions, each as a separate book entry:
- **Standard**: `pg<ID>` (750 words per chunk)
- **Short**: `pg<ID>_short` (325 words per chunk)
- **Long**: `pg<ID>_long` (1500 words per chunk)

All editions of the same book share a `parent_id` field (the base ID without suffix). This enables tracking which books a user has read across editions.

### Security & Authentication
- **Email Encryption**: User emails are encrypted at rest using Fernet symmetric encryption
- **Token-based Auth**: Uses `itsdangerous.URLSafeSerializer` for magic links
  - `binge_token`: Used for dashboard access and "read next" functionality
  - `unsub_token`: Used for unsubscribe links
- **Password Hashing**: Uses Argon2 via passlib for claimed accounts
- **Account States**:
  - "Ghost" users: Email only, created at signup
  - "Claimed" users: Have username and password for login

### Email Dispatch System (`src/dispatch.py`)

The dispatch system has two operating modes:

1. **Cron Mode** (automated):
   - Triggered hourly by cron
   - Checks user timezone and delivery_hour preference
   - Ensures only one email per day per subscription

2. **Force/Binge Mode** (manual):
   - Triggered by user clicking "Send Next Part" button
   - Rate-limited to once per 5 minutes
   - Bypasses timezone and daily frequency checks

Special logic:
- When a book is completed, sends a "victory" email with AI-generated recommendations
- Automatically activates the next queued book when one finishes
- Generates AI-powered recaps for parts 2+

### AI Integration (`src/ai.py`)

Uses Google Gemini API (`gemini-2.0-flash` model) for:
- **Recaps**: 2-3 sentence summaries of previous chunks (includes retry logic for rate limits)
- **Recommendations**: Selects 3 books from available library based on reading history
- **Blurbs**: Generated during ingestion for book descriptions

Key implementation details:
- Includes exponential backoff for rate limit handling
- Uses safety settings to prevent censoring of classic literature
- Filters recommendations by `parent_id` to avoid suggesting books already read

### Book Ingestion (`src/ingest.py`)

Primary script for adding new books to the system. Process:
1. Fetches text from Project Gutenberg (accepts `pg<ID>`, URL, or file with list)
2. Extracts title and author from Gutenberg metadata
3. Cleans text (removes Gutenberg headers/footers)
4. Downloads cover image from Gutenberg
5. Generates AI blurb/description
6. Chunks text into three editions (standard/short/long)
7. Inserts chunks and book metadata into MongoDB

```bash
# Ingest single book by Gutenberg ID
docker exec -it dailylitbits python ingest.py pg11

# Ingest from URL
docker exec -it dailylitbits python ingest.py https://www.gutenberg.org/cache/epub/11/pg11.txt

# Bulk ingest from file
docker exec -it dailylitbits python ingest.py book_list.txt

# Override metadata
docker exec -it dailylitbits python ingest.py pg11 --title "Custom Title" --author "Author Name"
```

## Important Patterns & Conventions

### Database Dependency Injection
Always use the dependency injection pattern for database access:

```python
@app.get("/route")
async def route_handler(db: MongoClient = Depends(get_db)):
    # Use db here
    books = list(db.books.find(...))
```

### Subscription Status Flow
- `active`: Currently receiving daily parts
- `queued`: Waiting for active book to finish
- `paused`: User-initiated pause (vacation mode)
- `completed`: Book finished
- `unsubscribed`: User opted out

Only ONE subscription per user can be `active` at a time. Others must be `queued`.

### Token Generation Pattern
All magic links use subscription_id as the payload:

```python
token = security.generate_binge_token(subscription_id)
dashboard_link = f"https://dailylitbits.com/profile?token={token}"
```

### Timezone Handling
- User timezone stored in `users.timezone` (defaults to 'UTC')
- Delivery hour stored in `subscriptions.delivery_hour` (defaults to 6 AM)
- All timestamps in database stored in UTC
- Dispatch logic converts to user's timezone for delivery time checks

## Configuration & Environment

The `.env` file (not committed to git) contains:
- `MONGO_URI`: MongoDB connection string
- `DB_NAME`: Database name
- `ENCRYPTION_KEY`: Fernet key for email encryption (must be 32 bytes, base64-encoded)
- `GEMINI_API_KEY`: Google Gemini API key
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`: Brevo SMTP credentials
- `FROM_EMAIL`: Sender email address

## Cron Jobs

The application uses system cron (not Docker-based) for scheduled tasks:

```bash
# Hourly email dispatch
0 * * * * docker exec dailylitbits python dispatch.py

# Daily AI processing (at 8 PM)
0 20 * * * docker exec dailylitbits python summarize.py

# Daily database backup (at 2 AM)
0 2 * * * /usr/bin/python3 /root/dailyLitBits/src/backup.py
```

## Useful Utility Scripts

Located in `src/tools/`:
- `audit_library.py`: Check library integrity, find orphaned chunks
- `enhance_library.py`: Regenerate AI descriptions for existing books
- `model_list.py`: List available Gemini models
- `send_file_email.py`: Test email delivery

## Common Development Workflows

### Adding a New Book
1. Find the Project Gutenberg ID (e.g., "11" for Alice in Wonderland)
2. Run ingestion: `docker exec -it dailylitbits python ingest.py pg11`
3. Verify in database or visit `/library` page

### Testing Email Dispatch
1. Find a subscription ID from the admin dashboard or database
2. Force send: `docker exec -it dailylitbits python dispatch.py force <sub_id>`
3. Check logs for errors

### Debugging Failed Tests
Tests use a temporary database (`test_db_<random>`). If tests fail:
1. Check that MongoDB is accessible from container
2. Ensure `.env` has valid credentials
3. Look for test database cleanup issues (old test DBs lingering)

### Modifying Code
1. Edit code in `src/` directory (changes are live-mounted to container)
2. Restart the web service: `docker compose -f docker/docker-compose.yml restart web`
3. Run tests to verify: `docker exec -it dailylitbits pytest tests/`

## Important Constraints

1. **Never run Python commands on the host** - always use `docker exec`
2. **Rebuild after dependency changes** - `docker compose build` after modifying `docker/requirements.txt`
3. **Restart after code changes** - Web server must be restarted to pick up Python changes
4. **Don't commit secrets** - `.env` file and `src/static/covers/` are git-ignored
5. **Email encryption is required** - All user emails must be encrypted before storage
6. **One active subscription per user** - Enforced in signup logic
7. **Timezone-aware timestamps** - Use `pytz` for all timezone operations
8. **Rate limiting on binge reads** - 5-minute cooldown between manual dispatches
