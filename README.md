# dailyLitBits

A public-domain book, a few hundred words a day, by email.

[dailylitbits.com](https://dailylitbits.com) splits books from Project
Gutenberg into 325-word chunks and sends a couple of them each morning, at an
hour the reader picks. Every email carries a recap of the story so far, so
coming back after a few days away doesn't mean starting the chapter again.

## How it works

- **FastAPI and SQLite**, in one Docker container. A single database file
  holds the library and the subscriptions; a nightly job gzips it, checks its
  integrity and counts what's inside before keeping it.
- **No passwords.** A reader is an email address. Signing in means asking for
  a link, which is good for an hour.
- **One chunking per book**, at `ingest.CHUNK_TARGET_WORDS`. How much arrives
  per email is `subscriptions.chunks_per_day`, and changing it doesn't move
  the reader's place. Boundaries fall between paragraphs, with a sentence-level
  split for the paragraphs that are longer than the target on their own.
- **Recaps are written once, at ingestion**, each one building on the last, so
  sending the mail costs nothing but the send.
- **The library grows itself.** A daily job considers Gutenberg's popularity
  ranking and takes at most one book. A deterministic filter settles what it
  can for free; only the genuinely ambiguous candidates reach a single
  adjudicator call. It fails closed — no key, a timeout or a spend cap means
  no book, never every book.

## Running it

Everything runs in the container; nothing is run with the host's Python.

```bash
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up -d
docker exec -it dailylitbits pytest tests/
```

Scripts take the same route — `docker exec -it dailylitbits python ingest.py pg11`
adds a book, and `dispatch.py`, `summarize.py` and `acquire.py` are the three
jobs cron runs. `cron.txt` is the schedule they run on.

Configuration is environment-only; `src/config.py` lists every variable, and
nothing sends mail without `SECRET_KEY`, `ENCRYPTION_KEY` and SMTP
credentials. `CLAUDE.md` is the working reference for anything deeper:
architecture, the auth model, backup and restore, the acquisition design, and
a dated incident log.
