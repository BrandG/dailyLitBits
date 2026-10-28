# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

dailyLitBits is a FastAPI web application that delivers public-domain
literature by email, a few hundred words a day. A reader subscribes to a book
and gets its next chunks each morning at an hour of their choosing, each one
preceded by a recap of the story so far. Everything is stored in one SQLite
file (`data/app.db`); Google Gemini writes the recaps, Jev adjudicates which
books the library takes on, and Brevo sends the mail. Identity is a magic
link - no password, and no separate admin login - and a reader manages their
subscriptions from a dashboard that link opens.

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

**SQLite**, one file: `data/app.db` on the host, `/data/app.db` in the
container (`../data:/data` in docker-compose, and `data/` is git-ignored).
Moved off the shared BlueHorseshoe MongoDB on 2026-10-25 - see the Incident
Log for why that coupling had to go. Nothing the website or cron runs touches
MongoDB any more.

- Schema: `src/schema.sql`, applied by `db.init_db()` and written to be
  idempotent. Tables: `users`, `books`, `chunks`, `subscriptions`,
  `suggestions`.
- Connections: `db.connect()` for a plain one, `db.get_db()` to also ensure
  the schema, `db.transaction(conn)` around a batch of writes. WAL mode,
  foreign keys on, 15s busy timeout.
- `db.connect()` passes `check_same_thread=False`, because FastAPI opens the
  connection on a worker thread and uses it on the event loop thread. That is
  safe only because a connection belongs to one request and is used serially
  within it. Do not share a connection across concurrent work.
- Several queries alias columns to the names the templates already use
  (`id AS book_id`, `blurb AS description`) rather than rename in templates.

Still on MongoDB and **not ported**: `src/tools/audit_library.py` and
`src/tools/enhance_library.py`. They carry a NOT PORTED banner and will find
an empty database. Rewrite against `db.py` or delete them.

### Chunk Size and Delivery Size

**Replaces the three-edition system** (Mongo-era: `pg11`, `pg11_short`,
`pg11_long` at 750/325/1500 words, linked by `parent_id`). That stored every
book three times, tripled recap generation, and lost the reader's place on an
edition switch because the sequence numbers did not line up.

In SQLite each book is chunked **once**, at `ingest.CHUNK_TARGET_WORDS` (325).
How much arrives per delivery is `subscriptions.chunks_per_day` (default 2,
roughly the old 750-word standard). Changing it does not move the reader's
place - it only changes how many chunks the next email carries.

Chunk boundaries fall between paragraphs, and `split_long_paragraph()` breaks
on sentence endings when a single paragraph is itself longer than the target.
Without that, Gutenberg's unbroken letters and contents blocks produced
2,480-word chunks in a 325-word scheme. Measured spread after the fix, over
three real books: 139-473 words, averaging 281.

### Security & Authentication

**One kind of user.** A reader is an email address. There is no username, no
password, no "claimed" state, and no separate admin login. Identity is a
signed link mailed to that address; `users.role` ('reader' or 'admin') is the
only permission.

- **Addresses** are Fernet-encrypted at rest (`users.email_enc`). Because
  Fernet output is non-deterministic the ciphertext cannot be searched, so
  `users.email_lookup` holds an HMAC of the normalised address - an indexed,
  unique lookup that keeps the address out of the clear. Never log a
  decrypted address; the old version printed them to stdout on every signup,
  which put them in `docker logs` and undid the encryption.
- **Tokens** (`src/security.py`), each with its own salt so one cannot be
  replayed as another:

  | Purpose | Carries | Lifetime |
  |---|---|---|
  | `dashboard` | user id | long - it lives in an inbox |
  | `unsubscribe` | subscription id | long |
  | `next` | subscription id | long |
  | `admin` | user id | 1 hour (`ADMIN_TOKEN_MAX_AGE`) |

- Signed with `SECRET_KEY`, deliberately **not** `ENCRYPTION_KEY`. One key for
  both would mean rotating it invalidated every link *and* made every stored
  address undecryptable at once.
- **Authorisation, not just authentication.** A dashboard token says who you
  are; any route acting on a subscription also checks that the subscription
  belongs to that user (`owned_subscription()`). A valid token for user A
  cannot touch user B's subscription.
- **Getting back in**: `/login` takes an email and mails the dashboard link.
  It answers identically for known and unknown addresses, so it cannot be
  used to test whether someone is subscribed.
- **Admin**: `/admin` needs a fresh admin token *and* `role = 'admin'`.

```bash
# Make someone an admin (--create if they have never signed up)
docker exec dailylitbits python tools/admin.py promote you@example.com --create

# Mint a login link, valid one hour
docker exec dailylitbits python tools/admin.py link you@example.com

docker exec dailylitbits python tools/admin.py list
docker exec dailylitbits python tools/admin.py demote someone@example.com
```

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
5. Generates AI blurb/description (skip with `--no-ai`)
6. Chunks text once at 325 words (`CHUNK_TARGET_WORDS`)
7. Inserts book and chunks into SQLite, replacing any earlier copy of that
   book (`ON DELETE CASCADE` clears its chunks), so re-ingesting is safe
8. Recaps are **not** generated unless asked for - see below

```bash
# Ingest single book by Gutenberg ID
docker exec -it dailylitbits python ingest.py pg11

# Ingest from URL
docker exec -it dailylitbits python ingest.py https://www.gutenberg.org/cache/epub/11/pg11.txt

# Bulk ingest from file
docker exec -it dailylitbits python ingest.py book_list.txt

# Override metadata
docker exec -it dailylitbits python ingest.py pg11 --title "Custom Title" --author "Author Name"

# Text only, no API calls at all
docker exec -it dailylitbits python ingest.py pg11 --no-ai

# Ingest and generate recaps in one pass
docker exec -it dailylitbits python ingest.py pg11 --recaps

# Backfill recaps for a book already stored; no download, no re-chunking
docker exec -it dailylitbits python ingest.py pg11 --recaps-only

# Measure before committing to a big run: stop after N chunks
docker exec -it dailylitbits python ingest.py pg11 --recaps-only --limit 40
```

#### Recaps are opt-in on purpose

`chunks.recap` holds a running "story so far" through that chunk, so a
delivery starting at chunk S reads chunk S-1's recap whatever
`chunks_per_day` is. That flexibility is why **every** chunk needs a recap,
and the chain means the calls are sequential within a book (though different
books are independent and can run in parallel).

`generate_recaps_for_book()` resumes after the last chunk that already has a
recap and stops at the first failure rather than leaving a hole in the chain.

Measured 2026-10-25 on `gemini-3.5-flash-lite`, 41 chained calls on pg11:
0.94s per call, 465 input and 51 output tokens per call. For the 100 books in
`book_list.txt` that projects to roughly 28,000 calls, about 7 hours
sequential and single-digit dollars.

### Daily Acquisition (`src/acquire.py`, `src/adjudicator.py`)

One book a day, taken from Project Gutenberg's own popularity ranking. The job
walks the ranking from the top, skips everything already held or already
judged, ingests the first candidate that survives two filters, and stops.

```bash
# What the job would do, without downloading or recording anything.
# Still costs the adjudicator calls (fractions of a cent).
docker exec dailylitbits python acquire.py --dry-run --verbose

# One book, recaps and all. This is what cron runs.
docker exec dailylitbits python acquire.py

# What has been decided so far. No network, no cost.
docker exec dailylitbits python acquire.py --report

# Catch up by several books at once, or walk deeper into the ranking.
docker exec dailylitbits python acquire.py --count 5 --max-pages 20
```

**Stage one is deterministic and free**, and it settles most candidates: media
type, copyright flag, language, the presence of a plain-text file,
reference-work keywords in the title or in the catalogue's own subject
headings, periodical issue markers, and an exact match on the normalised
title. Rank 6 on the live ranking is a dictionary and rank 13 is a Shakespeare
omnibus, so this stage pays for itself immediately.

Two things the keyword list deliberately does **not** contain: `number` (the
issue-number regex already requires digits, and *The Number of the Beast* is a
novel) and `atlas` (*Atlas Shrugged*). A false reject is silent and costs a
book, so `test_acquire.py` carries a standing list of real titles that must
keep passing.

**Stage two is one Jev call**, and only for what stage one genuinely cannot
settle - a title that might be the same work under another name, a
translation, an omnibus wearing a singular title. See the adjudicator notes
below.

Every decision, including every rejection, is written to the `acquisitions`
table. That is what keeps day two cheap: the ranking barely moves, so a
candidate judged once is never paid for again. `failed` is the one decision
that is retried, because it means a transient download problem rather than a
verdict.

**Fail closed, and note which way closed points.** Skipping a good book costs
nothing - there are 79,000 more and another run tomorrow. Admitting a
dictionary pollutes a library people read for pleasure. So every failure path
(no key, timeout, spend cap, malformed answer, low confidence) takes no book.
With no `JEV_API_KEY` configured the job acquires **nothing**; it does not fall
back to accepting everything.

#### The adjudicator (`src/adjudicator.py`)

Jev is TypeSafe AI's non-generative decision model: it takes application state
and typed questions and returns a choice with a calibrated probability. That
is a better fit here than a chat model, because "is this the same work?" wants
a number to threshold on rather than prose to parse. The same key and SDK are
used by the sibling Ricky project.

Calibration facts, measured against the live API on 2026-10-27. A
default-guessing implementation gets all of these wrong:

- Questions take `instructions`, not `prompt`. `Choice.criteria` is a dict of
  label to meaning; `Score.criteria` is a flat list of labels.
- **The conviction scale is question-dependent.** On this question it measured
  1.04-2.44 (median 2.01). On Ricky's much harder question it measured
  0.39-1.74 (median 0.88), so a threshold borrowed from there - or the obvious
  `min_conviction=2` - would reject nearly everything. The gate here is 1.4,
  calibrated on this question's own distribution.
- **Not deterministic.** Same state, repeated: the choice is stable but the
  confidence moves. Here that only matters for a candidate sitting exactly on
  a gate, where the two outcomes are "take it today" and "take it tomorrow".
- Cost is negligible: 742 input tokens median, about three thousandths of a
  cent per call.

**Validated before it was trusted**, because a clean sweep on known-answer
cases proves nothing on its own:

| check | result |
|---|---|
| 16 known-answer cases (editions, translations, same-author-different-book, dictionaries, periodicals) | 16/16 |
| C1 empty `already_held` - every DUPLICATE must flip to NEW | 5/5 flipped |
| C2 obscure title, matching holding swapped in underneath it | NEW then DUPLICATE |
| C3 labels renamed to ALPHA/BETA/GAMMA, same meanings | 5/5 survived |
| C4 invented nonsense titles | conviction 1.04-1.23, well under the gate |

C1 is the one that matters: without it, "Moby-Dick is a duplicate" is
indistinguishable from recognising a famous title. The gates were then checked
against 22 live verdicts and fall in a gap rather than through a cluster -
everything accepted scored 1.50 and above, everything gated scored 1.36, 1.38,
1.38. What the probability gate catches in practice is short-story
collections, where NEW and NOT_SERIAL are both defensible; declining to decide
those is the right outcome.

#### One a day, and what stops it

The job keeps walking until it actually takes a book. Rejections never end a
run - that was always true - but two things used to end one for reasons that
had nothing to do with any book, and both are fixed:

- **A dead catalogue page took the rest of the ranking with it.** The walker
  followed gutendex's own `next` links, and the link to page N+1 only arrives
  inside page N, so one timeout ended the walk. It now walks numbered URLs
  (`?sort=popular&page=N`), so a dead page is stepped over. Rank is derived
  from the page number, so a skipped page does not shift the ranks below it.
  Four consecutive dead pages is treated as the catalogue being down, which is
  a different thing from the ranking being exhausted, and is reported as such.
- **The 12-page bound stopped the search at ~384 candidates** whether or not a
  book had been found. The bound is now 60 pages, about 1,900 candidates. On a
  normal day none of it is walked - the ranking is sorted by popularity and the
  job stops the moment it has a book, so a typical run reads two pages.

**Persisting means looking further down the ranking, never relaxing a filter.**
A day with no book is still the right outcome when there is genuinely nothing
left to take, and the gates and the fail-closed paths are untouched. The
consequence to accept is that as the library grows, the books get less famous:
one a day is bought by going deeper.

The bounds that can end a run early, all of them reported by name in the final
log line, because the one thing a quiet daily job must not do is look the same
when it is working as when it is broken:

| bound | value | what it means |
|---|---|---|
| `MAX_PAGES` | 60 pages | `page_limit` - more ranking below; raise `--max-pages` |
| `PAGE_FAILURE_LIMIT` | 3 dead pages in a row | `catalogue_unreachable` - gutendex, not us |
| `SEARCH_BUDGET_S` | 1800s | `search_budget`; excludes ingest and recap time |
| `MAX_ADJUDICATIONS` | 120 per run | `adjudication_limit`; ~$0.005 at the far end |
| `ADJ_FAILURE_LIMIT` | 6 errors in a row | `adjudicator_outage` - take nothing on its word |
| `ERROR_LIMIT` | 8 unexpected errors | `too_many_errors` |

Reaching the end cleanly reports `end_of_catalogue`; a successful run reports
`took_target`. `catalogue_unreachable` is the one worth noticing, because it
is the only outcome that says nothing about the books.

**Catalogue pages have their own timeout, and it is large.** Measured
2026-10-28: page 1 returns in 0.1s, page 5 in **100.9s**, both with perfectly
good data - the shallow pages are evidently cached and the deep ones computed.
At the old shared `HTTP_TIMEOUT` of 30s a deep walk would have failed page five
three times over and declared the catalogue unreachable while it was working
fine, which would have made the 60-page bound fiction. `CATALOGUE_TIMEOUT` is
150s with two attempts rather than three: when one attempt can cost two
minutes, a third is budget spent on the same answer. `HTTP_TIMEOUT` stays at
30s for book downloads, which are a different kind of request.

So the real governor on a deep walk is `SEARCH_BUDGET_S`, not the page bound.
At worst-case page latency that is a dozen or so pages; on a normal day, when
pages come back in a tenth of a second, it is the full 60.

#### Removed by hand

The library was seeded from `src/book_list.txt`, written before this filter
existed, and one entry was not a book: `pg3206`, 'Moby Multiple Language Lists
of Common Words', a vocabulary dataset with a documentation header. Removed
2026-10-28, with no subscriptions pointing at it. Three things went with it -
the chunks (by cascade), the cover file, and the entry in the seed list, which
is commented out rather than deleted so a future bulk re-run cannot quietly
put it back.

The `acquisitions` table carries a `not_serial` row for 3206 with reason
`removed_by_hand:word_list_from_original_book_list`. That reason string is not
written by any code path; it exists so the audit trail explains a gap in the
popularity walk. Belt and braces, as it turns out - stage one rejects the
title unaided, on the phrase `lists of common words`.

#### Two bugs the first live run found

1. Gutenberg's catalogue leaks **MARC subfield delimiters** into titles: the
   live ranking served `The silent Baltic : $b or, Detained near Kiel`. That
   string would have been stored and shown on the website.
   `clean_catalogue_title()` runs before the title is stored, not only before
   it is compared.
2. **Ligatures have no canonical decomposition**, so NFKD left them alone and
   the punctuation strip turned `Encyclopaedia` into `encyclopdia`, which
   matched no keyword. Mapped explicitly now.

## Important Patterns & Conventions

### Database Dependency Injection
Always use the dependency injection pattern for database access:

```python
@app.get("/route")
async def route_handler(conn=Depends(get_db)):
    books = conn.execute("SELECT id AS book_id, title FROM books ORDER BY title").fetchall()
```

The dependency hands out one connection per request and closes it afterwards.

### Subscription Status Flow
- `active`: Currently receiving daily parts
- `queued`: Waiting for active book to finish
- `paused`: User-initiated pause (vacation mode)
- `completed`: Book finished
- `unsubscribed`: User opted out

Only ONE subscription per user can be `active` at a time. Others must be `queued`.

### Token Generation Pattern
The payload depends on what the link is for: the dashboard identifies a
*user*, the links inside an email identify a *subscription*. Mint with the
matching function - the salts mean a token is useless for any other purpose
(see Security & Authentication).

```python
# A way back in to everything this reader has
dashboard_link = f"https://dailylitbits.com/profile?token={security.generate_dashboard_token(user_id)}"

# Links that belong to one subscription, for inside its email
next_link  = f"https://dailylitbits.com/next?token={security.generate_next_token(sub_id)}"
unsub_link = f"https://dailylitbits.com/unsubscribe?token={security.generate_unsub_token(sub_id)}"
```

Any route that acts on a subscription must also check it belongs to the
token's user - `owned_subscription(conn, sub_id, user_id)` in `main.py`.

### Timezone Handling
- User timezone stored in `users.timezone` (defaults to 'UTC')
- Delivery hour stored in `subscriptions.delivery_hour` (defaults to 6 AM)
- All timestamps in database stored in UTC
- Dispatch logic converts to user's timezone for delivery time checks

## Configuration & Environment

The `.env` file (not committed to git) contains:
- `MONGO_URI`: MongoDB connection string. Must carry the `dlbapp` credentials and
  `authSource=slow_reader_db` - the shared mongod requires authentication (see Incident Log).
  Keep this file at mode 600.
- `DB_NAME`: MongoDB database name. Only the two unported `tools/` scripts read it.
- `DB_PATH`: SQLite file. Defaults to `<project>/data/app.db`, which resolves
  correctly both on the host and in the container - leave it unset.
- `ENCRYPTION_KEY`: Fernet key for email encryption (must be 32 bytes, base64-encoded)
- `SECRET_KEY`: signs every magic link and keys the `email_lookup` HMAC.
  Changing it logs everyone out **and** orphans every `email_lookup` value, so
  it is not a routine rotation. Separate from `ENCRYPTION_KEY` on purpose.
- `GEMINI_API_KEY`: Google Gemini API key
- `GEMINI_MODEL`: Gemini model id (default `gemini-3.5-flash-lite`). Google
  retires these; `gemini-2.0-flash` was withdrawn and every AI feature
  returned 404 until this was changed. List what the key can see with
  `docker exec dailylitbits python tools/model_list.py`.
- `JEV_API_KEY`: TypeSafe AI key for the acquisition adjudicator. **Unset is a
  supported state**: `acquire.py` then takes no book at all rather than taking
  every book. Shared with the Ricky project, so the two share a daily cap.
- `JEV_MODEL`: pinned Jev model id (default `jev-1.13.0`). Pinned because this
  is a judgment layer - a silent upgrade would change what the library admits
  with nothing appearing to break, and the conviction scale is model-specific.
- `JEV_DAILY_USD_CAP`: bound on a bug, not a budget (default 0.25). A normal
  run costs well under a cent, so anything near this cap means something is
  looping.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`: Brevo SMTP credentials
- `FROM_EMAIL`: Sender email address

## Cron Jobs

The application uses system cron (not Docker-based) for scheduled tasks:

```bash
# Hourly email dispatch
0 * * * * docker exec dailylitbits python dispatch.py

# Daily recap report (at 8 PM). summarize.py REPORTS by default and generates
# nothing without --run, so this entry logs what is outstanding rather than
# spending the Gemini budget unattended. Do not add --run to the crontab.
0 20 * * * docker exec dailylitbits python summarize.py

# Daily database backup (at 2 AM). Runs on the host, against the SQLite file
# through the bind mount - one of the few things that is NOT run via docker exec.
0 2 * * * /usr/bin/python3 /root/dailyLitBits/src/backup.py

# Daily acquisition (at 4 AM). Takes at most one book, and exits 0 when it
# takes none - on most days there is nothing new in the top few hundred, and a
# cron job that cries wolf gets ignored. Deliberately after the backup, so a
# bad acquisition is always one restore away from being undone.
0 4 * * * docker exec dailylitbits python acquire.py
```

## Backup and Restore

`src/backup.py` uses SQLite's online backup API (safe while the app is
serving), then **opens the copy and runs `PRAGMA integrity_check` before
calling it a success**, logging the book/chunk/user counts it found. An
archive under that bar is a failure, and a failed run does not clean up older
backups. Kept in `~/backups` for `RETENTION_DAYS` (3), gzipped.

That verification exists because the Mongo-era version wrote 23-byte files
containing an authentication error every night for seven months while
reporting success.

To restore (there is no `sqlite3` CLI on this host, so use python):

```bash
gunzip -c ~/backups/dailylitbits_YYYY-MM-DD_HH-MM-SS.db.gz > /tmp/restored.db
/usr/bin/python3 -c "import sqlite3; c=sqlite3.connect('/tmp/restored.db'); \
  print(c.execute('PRAGMA integrity_check').fetchone()[0]); \
  print(c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0], 'chunks')"
# then, with the app stopped:
docker compose -f docker/docker-compose.yml stop web
cp /tmp/restored.db data/app.db && rm -f data/app.db-wal data/app.db-shm
docker compose -f docker/docker-compose.yml start web
```

Verified end to end on 2026-10-25: backed up, restored, integrity ok, content
intact. Re-check it occasionally - that is the whole point.

## Useful Utility Scripts

Located in `src/tools/`:
- `admin.py`: promote / demote an admin, mint a one-hour `/admin` link, list users
- `model_list.py`: List available Gemini models - run this first when an AI
  feature starts returning 404
- `send_file_email.py`: Test email delivery
- `audit_library.py`, `enhance_library.py`: **NOT PORTED.** Still MongoDB; they
  will find an empty database. Rewrite against `db.py` or delete.

## Common Development Workflows

### Adding a New Book
1. Find the Project Gutenberg ID (e.g., "11" for Alice in Wonderland)
2. Run ingestion: `docker exec -it dailylitbits python ingest.py pg11`
   (add `--no-ai` to skip the blurb call; recaps never run unless asked)
3. Visit `/library` to check it appears
4. Recaps, when you want them: `ingest.py pg11 --recaps-only`. Check the
   outstanding total across the library first with `summarize.py`.

### Testing Email Dispatch
1. Find a subscription ID from the admin dashboard or database
2. Build the email without sending it:
   `docker exec -it dailylitbits python dispatch.py force <sub_id> --dry-run`
3. Send it for real: `docker exec -it dailylitbits python dispatch.py force <sub_id>`
4. `dispatch.py` with no arguments is the hourly cron pass; it reports why each
   subscription was skipped, which is usually "not delivery time".

### Debugging Failed Tests
Each test run gets its own SQLite file in a temp directory - `conftest.py`
sets `DB_PATH` before the app modules are imported, so tests never touch
`data/app.db` and need no database permissions. Email is never sent: the
`sent` fixture replaces `dispatch.send_via_brevo` with a recorder, and tests
assert against what would have gone out.

If tests fail, check that `.env` has `ENCRYPTION_KEY` and `SECRET_KEY` - both
are read at import time and will fail the whole run if missing.

### Modifying Code
1. Edit code in `src/` directory (changes are live-mounted to container)
2. Restart the web service: `docker compose -f docker/docker-compose.yml restart web`
3. Run tests to verify: `docker exec -it dailylitbits pytest tests/`

## Important Constraints

1. **Never run Python commands on the host** - always use `docker exec`. The
   one deliberate exception is `backup.py`, which cron runs on the host so the
   backup lands outside the container.
2. **Rebuild after dependency changes** - `docker compose build` after modifying `docker/requirements.txt`
3. **Restart after code changes** - Web server must be restarted to pick up Python changes
4. **Don't commit secrets** - `.env` file and `src/static/covers/` are git-ignored
5. **Email encryption is required** - All user emails must be encrypted before
   storage, and never written to a log
6. **One active subscription per user** - Enforced in signup logic; the rest queue
7. **Changing `chunks_per_day` must never reset progress** - that was the bug in
   the old edition switcher, and there is a test for it
8. **Timezone-aware timestamps** - Use `pytz` for all timezone operations
9. **Rate limiting on binge reads** - 5-minute cooldown between manual dispatches
10. **The database is ours, and should stay that way** - `data/app.db` is this
    project's own SQLite file, bind-mounted from the host. Neither the website
    nor any cron job touches the shared BlueHorseshoe mongod any more; the only
    things still pointed at it are the two unported `tools/` scripts, and
    `MONGO_URI` exists for them alone. Nothing new here should depend on a
    container another stack owns - that coupling is what cost seven months of
    downtime (see Incident Log).

## Incident Log

### 2026-04-03 to 2026-10-25 - HTTP 500 on every request (shared Mongo began requiring auth)

**Symptom.** Every request to dailylitbits.com returned 500 for roughly seven months.
`docker logs dailylitbits` ended in
`pymongo.errors.OperationFailure: Command find requires authentication` (code 13).
nginx was healthy and `curl http://127.0.0.1:8002/` reproduced the 500 directly, so the
fault was in the app. The container itself was up and had never crashed, which is why
nothing looked wrong from `docker ps`.

**Root cause.** This project does not run its own database. It shares the MongoDB
container owned by the BlueHorseshoe stack
(`/root/BlueHorseshoe/docker/docker-compose.yml`, joined through the external
`docker_app_net` network). On 2026-04-03 BlueHorseshoe enabled authentication on that
mongod: `--auth` was added to the mongod command and a `bhapp` user was created with
readWrite on the `bluehorseshoe` database (recorded in BlueHorseshoe's `TODO.md`).
dailyLitBits' `MONGO_URI` was still the anonymous `mongodb://mongo:27017/`, so from the
moment that container was recreated every query was refused. First failure in the
container log: 2026-04-03 04:35 UTC. Nothing in this project changed - the outage came
from a config change in a different project that happens to own the database.

**Fix.** A dedicated `dlbapp` user with `readWrite` on `slow_reader_db` only, and the
credentialled URI in `.env` (mode 600, git-ignored):
`MONGO_URI=mongodb://dlbapp:<password>@mongo:27017/slow_reader_db?authSource=slow_reader_db`.

That mongod has no user administrator - `bhapp` holds only readWrite on its own
database, and MongoDB's localhost exception closes as soon as any user exists - so the
new user had to be created with authentication off. If another user is ever needed:

```bash
docker stop mongo
docker run --rm -d --name mongo-noauth -v docker_mongo_data:/data/db mongo:7 \
  mongod --bind_ip 127.0.0.1 --wiredTigerCacheSizeGB 1
docker exec -it mongo-noauth mongosh slow_reader_db   # db.createUser({...})
docker stop mongo-noauth && docker start mongo
```

Creating a proper user-administrator account on that mongod would remove the need for
this dance; that is BlueHorseshoe's call, since it owns the database.

**Same root cause, also broken.** `src/backup.py` ran `mongodump --db slow_reader_db`
with no credentials, so since 2026-04-03 every nightly backup was a 23-byte file
containing nothing but the Unauthorized error. It now passes the `.env` `MONGO_URI`
through `--uri`, and treats an archive under 1 KB as a failure: a dump that cannot read
the database still exits 0 and writes a header-only archive of about 116 bytes, which is
exactly how seven months of empty backups went unnoticed.

**Still outstanding - the database is empty.** `slow_reader_db` does not exist on that
mongod: no `books`, `chunks`, `users` or `subscriptions`. This is older than the auth
break and has a different cause. The oldest backup still kept, from 2026-03-30, is
already a 116-byte empty archive, so the data was gone days before authentication was
turned on. Nothing is recoverable from this box: every archive in `/root/backups` is
empty, and the mongod log covering that period went with the container that was replaced
on 2026-04-03. The site now serves correctly but with an empty "Choose a Book" list.
The public-domain library can be rebuilt with `ingest.py` (see "Adding a New Book" and
`src/book_list.txt`); subscriber accounts and reading positions cannot.

**Resolved 2026-10-25.** The app now owns its data in SQLite and no longer
connects to that mongod at all, so a configuration change in BlueHorseshoe
cannot take this site down again. The scoped `dlbapp` Mongo user was left in
place: removing it needs another auth-off window on the shared database, and
it is a read/write grant on a database that no longer exists.


