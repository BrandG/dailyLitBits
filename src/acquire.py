"""Grow the library by one book a day, quietly and cheaply.

Walks Project Gutenberg's own popularity ranking from the top, skips everything
already held or already judged, and ingests the first candidate that survives
two filters. Then it stops. One book a day compounds to a library without ever
costing a day's attention.

It keeps walking until it has one. Rejections are not a reason to stop, and
neither is a catalogue page that times out: the walk steps over a dead page
rather than ending there. What it will not do is lower the bar to come back
with something - persisting means looking further down the ranking, so an
empty day is still the right answer when there is nothing left to take. Every
bound that can end a run early is reported by name in the final log line,
because a quiet daily job must not look the same when it is working as when it
is broken.

Two stages, in this order for a reason:

  1. **Deterministic, free.** Media type, copyright, language, a plain-text
     file, reference-work keywords in the title or the catalogue subjects, and
     an exact match on the normalised title. This settles the large majority of
     candidates at no cost. Rank 6 on the live ranking is a dictionary, so this
     stage earns its place immediately.
  2. **Adjudicated.** Only what stage one genuinely cannot settle — a title
     that might be the same work under another name, a translation, an omnibus
     wearing a singular title. One Jev call, about three thousandths of a cent.

Every decision is written to the `acquisitions` table, which is what keeps the
job cheap on day two: a candidate judged once is never paid for again. The
table is also the audit trail — why the library contains what it contains.

Cost per run, measured: one Jev call per undecided candidate (~$0.00004 each),
plus one Gemini recap call per chunk of the book actually taken. The judging is
rounding error; the recaps are the cost, and they scale with book length -
$0.08 for a 256-chunk novel, $0.44 for a 1,476-chunk one. Call it a dollar a
fortnight, and do not be surprised by a long book.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from logger import log

# Cron runs this from /root, so .env is named explicitly and loaded before
# config is imported. Same reason as backup.py.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

import adjudicator  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import events  # noqa: E402

# Gutendex is a JSON view over the Gutenberg catalogue that carries the
# download counts. Those counts are the ranking; the alternative is scraping
# the search pages, which breaks whenever the markup moves.
CATALOGUE = "https://gutendex.com/books/"
USER_AGENT = "dailyLitBits/1.0 (+https://dailylitbits.com)"

WANT_LANGUAGE = "en"
PAGE_SIZE = 32            # gutendex page size; fixed by the API, not by us
MAX_PAGES = 60            # ~1,900 candidates deep
MAX_TEXT_BYTES = 8 * 1024 * 1024
SHORTLIST = 8             # held titles sent to the adjudicator
HTTP_TIMEOUT = 30         # downloads and HEADs
# Catalogue pages get their own, far longer budget. Measured 2026-10-28:
# page 1 in 0.1s, page 5 in 100.9s, both with good data. Deep pages are slow,
# not broken, and a 30s cut threw away pages that work.
CATALOGUE_TIMEOUT = 150

# How far the job will go to come back with a book. The walk stops the moment
# it has one, so on a normal day none of these is reached - today's book was
# rank 44, on page 2. They are bounds on a bad day, not a budget for a good
# one.
#
# Every one of them is a reason a run can end empty, and each is reported by
# name, because "nothing new today" and "gutendex was down" look identical in
# a log that only says how many books were taken.
PAGE_FAILURE_LIMIT = 3    # consecutive dead pages => the catalogue is down
# Wall clock for searching; excludes ingest and recaps. Generous because a
# deep page can legitimately take two minutes, and this is a 04:00 job with
# nobody waiting. It has to stay comfortably above what PAGE_FAILURE_LIMIT
# dead pages cost, or a dead catalogue gets reported as a slow one.
SEARCH_BUDGET_S = 1800
MAX_ADJUDICATIONS = 120   # paid judgments per run; ~$0.005 at the far end
ADJ_FAILURE_LIMIT = 6     # consecutive adjudicator errors => an outage
ERROR_LIMIT = 8           # unexpected errors tolerated before giving up

# Leading articles, in the languages this library actually sees.
ARTICLES = ("the", "a", "an", "la", "le", "les", "el", "los", "las", "der",
            "die", "das", "il", "lo", "i", "gli", "de", "het", "een")

# Reference-work signals. Matched against the normalised title as whole words,
# so "dictionary" fires and "dictionaries of the heart" does not escape, while
# "contradiction" never fires.
REFERENCE_WORDS = {
    "dictionary", "dictionaries", "encyclopedia", "encyclopaedia", "thesaurus",
    "glossary", "lexicon", "concordance", "almanac", "almanack", "gazetteer",
    "bibliography", "catalogue", "catalog", "index", "indexes", "directory",
    "handbook", "wordlist", "phrasebook", "grammar", "primer", "textbook",
    "cookbook", "timetable", "statistics", "census", "proceedings",
    "transactions", "bulletin", "periodical", "magazine", "gutenberg",
    # Not here on purpose: "number" (the issue-number regex below wants
    # digits, and "The Number of the Beast" is a novel) and "atlas"
    # ("Atlas Shrugged"). A false reject is silent, so these stay narrow.
}
# Multi-word signals, matched as substrings of the normalised title.
REFERENCE_PHRASES = (
    "lists of common words", "list of common words", "complete works",
    "collected works", "complete poetical works", "works of", "anthology",
    "selected writings", "table of contents", "sheet music", "hymn book",
    "book of common prayer", "united states code", "patent office",
    "annual report", "reports of cases", "who's who", "year book",
    "yearbook", "notes and queries", "scientific american", "punch, or the",
    "the writings of",
)
# Catalogue subjects that settle it without reading the title.
REFERENCE_SUBJECTS = (
    "dictionaries", "encyclopedias", "bibliography", "indexes", "catalogs",
    "periodicals", "almanacs", "glossaries", "terminology",
    "english language -- dictionaries", "quotations",
)
# A periodical issue almost always names its number. These are strong enough
# on their own.
SERIAL_MARKERS = (
    r"\bvol(?:ume)?\.?\s*(?:[ivxlc]+|\d+)\b",
    r"\bno\.?\s*\d+\b",
    r"\bissue\s*\d+\b",
    r"\bpart\s*\d+\s*of\s*\d+\b",
    r"\bnumber\s*\d+\b",
)


# ---------------------------------------------------------------- normalising

# Ligatures have no canonical decomposition, so NFKD leaves them alone and the
# punctuation strip would silently eat them: 'Encyclopaedia' and
# 'Encyclopaedia' have to normalise alike.
LIGATURES = {"\u00e6": "ae", "\u0153": "oe", "\u00df": "ss", "\u00f8": "o",
             "\u0142": "l", "\u0111": "d", "\u00fe": "th", "\u00f0": "d"}


def strip_accents(s):
    s = "".join(LIGATURES.get(c, c) for c in (s or "").lower())
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def clean_catalogue_title(title):
    """Strip catalogue plumbing that would otherwise reach the website.

    Gutenberg's records carry MARC subfield delimiters ('$b' introduces a
    subtitle) and trailing separators. Seen live on the popularity ranking, so
    this runs before the title is stored, not only before it is compared.
    """
    t = (title or "").replace("\r", " ").replace("\n", " ")
    t = re.sub(r"\s*:\s*\$[a-z]\s*", ": ", t)   # 'X : $b or, Y' -> 'X: or, Y'
    t = re.sub(r"\$[a-z]\s*", "", t)             # any stragglers
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[\s:;,]+$", "", t)
    return t


def normalise_title(title):
    """Reduce a title to the thing two editions of one work have in common.

    'The Tragedie of Hamlet, Prince of Denmarke [Cambridge ed.]' and
    'Hamlet, Prince of Denmark' do not normalise to the same string - that is
    what the adjudicator is for. This only has to catch the mechanical cases:
    punctuation, accents, bracketed editorial matter, leading articles.
    """
    t = strip_accents(title or "").lower()
    t = re.sub(r"\[[^\]]*\]", " ", t)            # [Illustrated], [Cambridge ed.]
    t = re.sub(r"\([^)]*\)", " ", t)
    t = t.split(";")[0]                          # 'Moby Dick; Or, The Whale'
    t = re.sub(r"\bor,?\s", " ", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    words = [w for w in t.split() if w]
    while words and words[0] in ARTICLES:
        words.pop(0)
    return " ".join(words)


def surname(author):
    """Gutendex gives 'Austen, Jane'; the database holds 'Jane Austen'."""
    a = strip_accents(author or "").lower()
    a = re.sub(r"[^a-z,\s]", " ", a)
    if "," in a:
        return a.split(",")[0].strip()
    parts = [p for p in a.split() if p]
    return parts[-1] if parts else ""


def display_author(gutendex_authors):
    """'Austen, Jane' -> 'Jane Austen', which is the house style in `books`."""
    if not gutendex_authors:
        return "Unknown Author"
    name = gutendex_authors[0].get("name", "") or "Unknown Author"
    if "," in name:
        last, _, first = name.partition(",")
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


def title_tokens(norm):
    return {w for w in norm.split() if len(w) > 2}


# Why a run can come back with fewer books than asked for. Spelled out by
# name, because the one thing a quiet daily job must not do is look the same
# when it is working as when it is broken.
OUTCOME_NOTES = {
    "took_target": "",
    "end_of_catalogue": "walked the whole ranking; everything in it is "
                        "already held or already judged",
    "page_limit": "reached the page bound - there is more ranking below it, "
                  "so raise --max-pages to look deeper",
    "catalogue_unreachable": "gutendex was unreachable. Nothing to do with "
                             "the books; tomorrow's run tries again",
    "search_budget": "ran out of search time before finding one",
    "adjudication_limit": "hit the per-run judgment limit",
    "adjudicator_outage": "the adjudicator kept failing. Taking no book on "
                          "its word is the safe outcome, not a bug",
    "adjudicator_unavailable": "the adjudicator is not available, so nothing "
                               "was taken. This is the fail-closed path",
    "too_many_errors": "too many unexpected errors; look at the log above",
}


# ------------------------------------------------------------------ catalogue

def fetch_json(url, attempts=3, backoff=3.0, timeout=HTTP_TIMEOUT):
    """One slow page should not end the day's run.

    Observed on the first live dry run: gutendex timed out on page 2, the
    generator stopped, and the job finished having looked at five candidates.
    Retrying is the difference between "nothing new today" being true and
    being an artefact.
    """
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(backoff * (attempt + 1))
    raise last


def catalogue(max_pages=MAX_PAGES, pause=1.5, stats=None):
    """Yield (rank, record) down the popularity ranking, most downloaded first.

    Walks by page number instead of following the catalogue's own `next`
    links. Following `next` is the tidier use of the API, but it couples the
    pages together: the link to page N+1 only arrives inside page N, so one
    page that times out takes the entire rest of the ranking with it. That is
    what ended a run having looked at five candidates. Numbered URLs let a
    dead page be stepped over.

    A run of consecutive dead pages is a different thing from one dead page -
    it means the catalogue is down, and there is nothing to be gained by
    walking another fifty - so that stops the walk and says so.

    `stats` is filled in rather than returned, because this stays a generator:
    the caller needs to know afterwards whether the walk finished or was cut
    short, and by what.
    """
    if stats is None:
        stats = {}
    stats.setdefault("pages_read", 0)
    stats.setdefault("pages_failed", 0)
    stats["ended"] = "page_limit"

    consecutive = 0
    for page_no in range(1, max_pages + 1):
        try:
            page = fetch_json(f"{CATALOGUE}?sort=popular&page={page_no}",
                              attempts=2, timeout=CATALOGUE_TIMEOUT)
        except Exception as e:
            consecutive += 1
            stats["pages_failed"] += 1
            log(f"   [!] catalogue page {page_no} gave up after retries: "
                f"{type(e).__name__}: {e}."
                + (" Skipping it." if consecutive < PAGE_FAILURE_LIMIT else ""))
            if consecutive >= PAGE_FAILURE_LIMIT:
                stats["ended"] = "catalogue_unreachable"
                return
            time.sleep(pause)
            continue

        consecutive = 0
        stats["pages_read"] += 1
        results = page.get("results") or []
        for i, record in enumerate(results):
            # Derived from the page number, so a skipped page does not shift
            # every rank after it. The rank is stored on the decision.
            yield (page_no - 1) * PAGE_SIZE + i + 1, record

        if not page.get("next"):
            stats["ended"] = "end_of_catalogue"
            return
        time.sleep(pause)


def plain_text_url(record):
    """The plain-text format URL the catalogue advertises, if there is one."""
    formats = record.get("formats") or {}
    for mime, url in formats.items():
        if mime.startswith("text/plain") and not url.endswith(".zip"):
            return url
    return None


def head_size(url):
    """Content length, or None if the server will not say. Not fatal."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            value = r.headers.get("Content-Length")
            return int(value) if value else None
    except Exception:
        return None


# ------------------------------------------------------------- library state

def held_books(conn):
    """Everything in the library, normalised for comparison."""
    rows = conn.execute("SELECT id, title, author FROM books").fetchall()
    out = []
    for r in rows:
        norm = normalise_title(r["title"])
        out.append({
            "book_id": r["id"],
            "title": r["title"],
            "author": r["author"],
            "norm": norm,
            "tokens": title_tokens(norm),
            "surname": surname(r["author"]),
        })
    return out


def held_gutenberg_ids(conn):
    ids = set()
    for r in conn.execute("SELECT id FROM books").fetchall():
        m = re.fullmatch(r"pg(\d+)", r["id"] or "")
        if m:
            ids.add(int(m.group(1)))
    return ids


def already_decided(conn):
    """Ids we have judged before. 'failed' is left out so transient download
    errors are retried on a later run; a verdict is never re-paid for."""
    return {r["gutenberg_id"] for r in conn.execute(
        "SELECT gutenberg_id FROM acquisitions WHERE decision != 'failed'"
    ).fetchall()}


def record_decision(conn, gid, *, title, author, decision, reason, stage,
                    rank=None, downloads=None, probability=None,
                    conviction=None, model=None):
    conn.execute(
        """INSERT INTO acquisitions
               (gutenberg_id, title, author, decision, reason, stage,
                popularity_rank, downloads, probability, conviction, model,
                decided_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(gutenberg_id) DO UPDATE SET
               decision = excluded.decision,
               reason = excluded.reason,
               stage = excluded.stage,
               popularity_rank = excluded.popularity_rank,
               downloads = excluded.downloads,
               probability = excluded.probability,
               conviction = excluded.conviction,
               model = excluded.model,
               decided_at = excluded.decided_at""",
        (gid, title, author, decision, reason, stage, rank, downloads,
         probability, conviction, model, events.utc_now()),
    )


# ------------------------------------------------------------ stage one: free

def screen(record):
    """The deterministic filter.

    Returns (decision, reason) to settle it now, or (None, shortlist_reason)
    to pass it to the adjudicator.
    """
    title = clean_catalogue_title(record.get("title"))
    norm = normalise_title(title)
    subjects = [s.lower() for s in (record.get("subjects") or [])]
    shelves = [s.lower() for s in (record.get("bookshelves") or [])]

    if (record.get("media_type") or "Text") != "Text":
        return "unsuitable", "media_type:" + str(record.get("media_type"))
    if record.get("copyright"):
        return "unsuitable", "still_in_copyright"
    langs = record.get("languages") or []
    if WANT_LANGUAGE not in langs:
        return "unsuitable", "language:" + ",".join(langs or ["none"])
    if not plain_text_url(record):
        return "unsuitable", "no_plain_text"
    if not norm:
        return "unsuitable", "empty_title"

    words = set(norm.split())
    hit = words & REFERENCE_WORDS
    if hit:
        return "not_serial", "title_word:" + sorted(hit)[0]
    for phrase in REFERENCE_PHRASES:
        if phrase in norm:
            return "not_serial", "title_phrase:" + phrase
    for subject in subjects + shelves:
        for marker in REFERENCE_SUBJECTS:
            if marker in subject:
                return "not_serial", "subject:" + marker
    for pattern in SERIAL_MARKERS:
        if re.search(pattern, norm):
            return "not_serial", "serial_marker"

    return None, "needs_adjudication"


def exact_duplicate(norm, held):
    """A normalised title we already hold, with a matching surname."""
    for h in held:
        if h["norm"] and h["norm"] == norm:
            return h
    return None


def shortlist_for(norm, author_surname, held, n=SHORTLIST):
    """The plausible collision set: same author first, then title overlap.

    Duplicates live under the same author far more often than under a similar
    title, so author matches go in unconditionally. Sending the whole library
    would cost more and bury the handful that bear on the question.
    """
    tokens = title_tokens(norm)
    scored = []
    for h in held:
        same_author = bool(author_surname) and h["surname"] == author_surname
        overlap = len(tokens & h["tokens"]) / max(1, len(tokens | h["tokens"]))
        scored.append((1.0 if same_author else 0.0, overlap, h))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    picked = [(h["title"], h["author"]) for author_hit, overlap, h in scored
              if author_hit or overlap > 0]
    return picked[:n] or [(h["title"], h["author"]) for h in held[:n]]


# ------------------------------------------------------------------- the job

def run(*, count=1, max_pages=MAX_PAGES, dry_run=False, with_recaps=True,
        max_bytes=MAX_TEXT_BYTES, adj=None, conn=None, verbose=False):
    close_conn = conn is None
    conn = conn or db.get_db()
    adj = adj or adjudicator.from_config(config)

    try:
        held = held_books(conn)
        have_ids = held_gutenberg_ids(conn)
        decided = already_decided(conn)
        log(f"Library: {len(held)} books. Previously judged: {len(decided)}. "
            f"Adjudicator: {adj.name}. Target: {count} book(s).")

        taken = []
        examined = 0
        screened_out = 0
        adjudicated = 0
        adj_failures = 0
        errors = 0
        spend = 0.0
        walk = {}
        outcome = None
        search_started = time.monotonic()
        ingest_seconds = 0.0

        for rank, record in catalogue(max_pages=max_pages, stats=walk):
            if len(taken) >= count:
                outcome = "took_target"
                break
            # The budget covers searching only. Recaps on a long book run to
            # twenty minutes, and that is work already won - it must not count
            # against the hunt for the next one.
            if time.monotonic() - search_started - ingest_seconds > SEARCH_BUDGET_S:
                outcome = "search_budget"
                break
            if adjudicated >= MAX_ADJUDICATIONS:
                outcome = "adjudication_limit"
                break
            try:
                gid = record.get("id")
                if gid is None or gid in have_ids or gid in decided:
                    continue
                examined += 1

                title = clean_catalogue_title(record.get("title"))
                author = display_author(record.get("authors"))
                norm = normalise_title(title)

                decision, reason = screen(record)
                if decision:
                    screened_out += 1
                    if verbose:
                        log(f"   #{rank:<4} skip  {title[:52]!r} -> {decision} ({reason})")
                    if not dry_run:
                        record_decision(conn, gid, title=title, author=author,
                                        decision=decision, reason=reason,
                                        stage="deterministic", rank=rank,
                                        downloads=record.get("download_count"))
                    continue

                dup = exact_duplicate(norm, held)
                if dup:
                    screened_out += 1
                    if verbose:
                        log(f"   #{rank:<4} skip  {title[:52]!r} -> duplicate of "
                            f"{dup['book_id']} (normalised title match)")
                    if not dry_run:
                        record_decision(conn, gid, title=title, author=author,
                                        decision="duplicate",
                                        reason="normalised_title:" + dup["book_id"],
                                        stage="deterministic", rank=rank,
                                        downloads=record.get("download_count"))
                    continue

                # Stage two. Only the genuinely undecided reach here.
                cand = adjudicator.Candidate(
                    gutenberg_id=gid, title=title, author=author,
                    language=WANT_LANGUAGE,
                    subjects=record.get("subjects") or [],
                    bookshelves=record.get("bookshelves") or [],
                    downloads=record.get("download_count") or 0,
                )
                shortlist = shortlist_for(norm, surname(author), held)
                d = adj.judge(cand, shortlist)
                adjudicated += 1
                spend += d.cost_usd

                if not d.ok:
                    adj_failures += 1
                    log(f"   #{rank:<4} HOLD  {title[:52]!r} -> adjudicator "
                        f"unavailable ({d.reason}); taking no book on its word")
                    if d.reason in ("daily_spend_cap", "no JEV_API_KEY configured",
                                    "no_adjudicator_configured"):
                        outcome = "adjudicator_unavailable"
                        break
                    # One timeout is a candidate to skip. Six in a row is an
                    # outage, and walking another fifty pages of it just spends
                    # the timeout budget on the same answer.
                    if adj_failures >= ADJ_FAILURE_LIMIT:
                        outcome = "adjudicator_outage"
                        log(f"   [!] {adj_failures} adjudicator errors in a row.")
                        break
                    continue

                adj_failures = 0

                verdict_line = (f"{d.verdict} p={d.probability:.3f} "
                                f"conv={d.conviction:.2f}")
                if not d.accept:
                    log(f"   #{rank:<4} skip  {title[:52]!r} -> {verdict_line}"
                        + (f" [{d.reason}]" if d.reason else ""))
                    settled = {"DUPLICATE": "duplicate",
                               "NOT_SERIAL": "not_serial"}.get(d.verdict)
                    # A gated-out NEW is not a verdict about the book, it is a
                    # verdict about our certainty. Do not write it down as
                    # permanent - leave it for a later run to look at again.
                    if settled and not dry_run:
                        record_decision(conn, gid, title=title, author=author,
                                        decision=settled, reason=d.reason or "jev",
                                        stage="jev", rank=rank,
                                        downloads=record.get("download_count"),
                                        probability=d.probability,
                                        conviction=d.conviction, model=d.model)
                    continue

                log(f"   #{rank:<4} TAKE  {title[:52]!r} -> {verdict_line}")
                if dry_run:
                    taken.append((gid, title, 0))
                    continue

                ingest_started = time.monotonic()
                ok, note = ingest_candidate(conn, record, title, author,
                                            with_recaps=with_recaps,
                                            max_bytes=max_bytes)
                ingest_seconds += time.monotonic() - ingest_started
                if not ok:
                    log(f"         ingest failed: {note}")
                    record_decision(conn, gid, title=title, author=author,
                                    decision="failed", reason=note, stage="jev",
                                    rank=rank, downloads=record.get("download_count"),
                                    probability=d.probability,
                                    conviction=d.conviction, model=d.model)
                    continue

                book_id, chunks = note
                record_decision(conn, gid, title=title, author=author,
                                decision="acquired", reason="jev:NEW", stage="jev",
                                rank=rank, downloads=record.get("download_count"),
                                probability=d.probability, conviction=d.conviction,
                                model=d.model)
                events.record(conn, "acquired", book_id=book_id, chunks=chunks,
                              downloads=record.get("download_count") or 0,
                              stage="jev")
                taken.append((gid, title, chunks))
                # The new book joins the comparison set immediately, so a second
                # book in the same run cannot duplicate the first.
                held = held_books(conn)
                have_ids.add(gid)
            # Blanket, deliberately. Every realistic failure is already
            # handled closer to where it happens - the adjudicator catches
            # its own, ingest_candidate returns a reason - so this is for the
            # unforeseen. The failure mode it replaces is a traceback out of
            # cron and a day with no book, which is a bad trade for one odd
            # catalogue record.
            except Exception as e:  # noqa: BLE001
                errors += 1
                log(f"   #{rank:<4} ERR   "
                    f"{str(record.get('title'))[:52]!r} -> "
                    f"{type(e).__name__}: {e}")
                if errors >= ERROR_LIMIT:
                    outcome = "too_many_errors"
                    break
                continue

        if outcome is None:
            outcome = walk.get("ended", "page_limit")

        failed = walk.get("pages_failed", 0)
        log(f"Done. examined {examined}, filtered {screened_out} free, "
            f"adjudicated {adjudicated} (${spend:.6f}), acquired {len(taken)}. "
            f"Walked {walk.get('pages_read', 0)} page(s)"
            + (f", skipped {failed} dead" if failed else "")
            + f". Ended: {outcome}.")
        for gid, title, chunks in taken:
            log(f"   + pg{gid} {title!r} ({chunks} chunks)")
        if len(taken) < count:
            note = OUTCOME_NOTES.get(outcome, "")
            log(f"   Took {len(taken)} of {count}"
                + (f" - {note}." if note else "."))
        return taken
    finally:
        if close_conn:
            conn.close()


def ingest_candidate(conn, record, title, author, *, with_recaps, max_bytes):
    """Download, chunk and store. Returns (True, (book_id, chunks)) or
    (False, reason)."""
    import ingest

    gid = record["id"]
    book_id = f"pg{gid}"
    cache_url = f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt"

    size = head_size(cache_url)
    source = str(gid)
    override_id = None
    if size is None:
        # The cache path is not always populated. Fall back to the URL the
        # catalogue advertises, keeping the pgNNNN id the rest of the app uses.
        advertised = plain_text_url(record)
        if not advertised:
            return False, "no_plain_text_url"
        size = head_size(advertised)
        source = advertised
        override_id = book_id

    if size is not None and size > max_bytes:
        return False, f"too_large:{size}"

    try:
        ingest.process_source(source, override_title=title,
                              override_author=author, override_id=override_id,
                              use_ai=True, with_recaps=with_recaps)
    except Exception as e:
        return False, f"{type(e).__name__}:{e}"

    row = conn.execute(
        "SELECT total_chunks FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row or not row["total_chunks"]:
        return False, "no_chunks_stored"
    return True, (book_id, int(row["total_chunks"]))


def report(conn=None):
    """What the job has decided so far. Read-only."""
    close_conn = conn is None
    conn = conn or db.get_db()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM books").fetchone()["c"]
        log(f"Library: {total} books.")
        rows = conn.execute(
            """SELECT decision, stage, COUNT(*) n FROM acquisitions
               GROUP BY decision, stage ORDER BY n DESC""").fetchall()
        if not rows:
            log("No acquisition decisions recorded yet.")
            return
        log("Decisions recorded:")
        for r in rows:
            log(f"   {r['decision']:<11} via {r['stage']:<14} {r['n']:>5}")
        recent = conn.execute(
            """SELECT gutenberg_id, title, decision, reason, decided_at
               FROM acquisitions WHERE decision = 'acquired'
               ORDER BY decided_at DESC LIMIT 10""").fetchall()
        if recent:
            log("Most recent acquisitions:")
            for r in recent:
                log(f"   {r['decided_at'][:10]}  pg{r['gutenberg_id']:<7} "
                    f"{(r['title'] or '')[:54]}")
    finally:
        if close_conn:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Acquire the most popular Gutenberg book the library does "
                    "not already hold.")
    parser.add_argument("--count", type=int, default=1,
                        help="How many books to take this run (default 1).")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help=f"Catalogue pages to walk, 32 per page "
                             f"(default {MAX_PAGES}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Judge candidates and report, but download "
                             "nothing and record nothing. Still costs the "
                             "adjudicator calls.")
    parser.add_argument("--no-recaps", action="store_true",
                        help="Ingest without generating recaps. Leaves the "
                             "book undeliverable until summarize.py --run "
                             "backfills it.")
    parser.add_argument("--max-bytes", type=int, default=MAX_TEXT_BYTES,
                        help="Refuse a plain-text file larger than this.")
    parser.add_argument("--report", action="store_true",
                        help="Print what has been decided so far and exit. "
                             "No network, no cost.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log every rejection, not just the interesting ones.")
    args = parser.parse_args()

    if args.report:
        report()
        sys.exit(0)

    taken = run(count=args.count, max_pages=args.max_pages,
                dry_run=args.dry_run, with_recaps=not args.no_recaps,
                max_bytes=args.max_bytes, verbose=args.verbose)
    # Exit 0 even when nothing was taken. The job is meant to come back with
    # a book every day and now walks until it does, so an empty run is worth
    # reading about - but the reason is in the log's "Ended:" line, where it
    # can say which of nine things happened, and an exit code cannot. A cron
    # job that cries wolf gets ignored, and this one has to stay quiet enough
    # to be trusted for years.
    sys.exit(0)
