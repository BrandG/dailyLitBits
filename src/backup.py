"""Nightly backup of the SQLite database.

Uses SQLite's online backup API, so it is safe while the app is serving, and
then opens the copy and runs an integrity check before calling it a success.

That verification is the point. The previous version shelled out to mongodump
without credentials and wrote 23-byte files containing nothing but an
authorisation error, every night for seven months, while reporting success.
A backup nobody has restored is just a file taking up space - the restore
command is in CLAUDE.md, and it is one line.
"""
import datetime
import glob
import gzip
import os
import shutil
import sqlite3
import sys
import time

from dotenv import load_dotenv

from logger import log

# Cron runs this from /root, so the .env has to be named explicitly, and it
# has to be loaded before config is imported.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

import config  # noqa: E402

BACKUP_DIR = os.path.expanduser("~/backups")
RETENTION_DAYS = 3


def create_backup():
    if not os.path.exists(config.DB_PATH):
        log(f"Error: no database at {config.DB_PATH}")
        return False

    os.makedirs(BACKUP_DIR, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    plain_path = os.path.join(BACKUP_DIR, f"dailylitbits_{stamp}.db")
    final_path = plain_path + ".gz"

    log(f"Starting backup: {os.path.basename(final_path)}...")

    try:
        source = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        target = sqlite3.connect(plain_path)
        with target:
            source.backup(target)
        source.close()
        target.close()
    except Exception as exc:
        log(f"Error creating backup: {exc}")
        if os.path.exists(plain_path):
            os.remove(plain_path)
        return False

    # Verify the copy before trusting it.
    try:
        check = sqlite3.connect(plain_path)
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
        books = check.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        chunks = check.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        users = check.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        check.close()
    except Exception as exc:
        log(f"Error: backup copy will not open: {exc}")
        os.remove(plain_path)
        return False

    if result != "ok":
        log(f"Error: integrity check on the backup said '{result}'. Keeping old backups.")
        os.remove(plain_path)
        return False

    with open(plain_path, "rb") as raw, gzip.open(final_path, "wb") as gz:
        shutil.copyfileobj(raw, gz)
    os.remove(plain_path)

    size_mb = os.path.getsize(final_path) / (1024 * 1024)
    log(f"Success! {os.path.basename(final_path)} ({size_mb:.2f} MB) - "
        f"integrity ok, {books} books, {chunks} chunks, {users} users")
    return True


def cleanup_old_backups():
    log(f"Cleaning up backups older than {RETENTION_DAYS} days...")
    cutoff = time.time() - (RETENTION_DAYS * 86400)

    removed = 0
    for path in glob.glob(os.path.join(BACKUP_DIR, "dailylitbits_*.db.gz")):
        if os.path.getmtime(path) < cutoff:
            os.remove(path)
            log(f"Deleted old backup: {os.path.basename(path)}")
            removed += 1

    if removed == 0:
        log("No old backups to delete.")


if __name__ == "__main__":
    if create_backup():
        cleanup_old_backups()
    else:
        sys.exit(1)
