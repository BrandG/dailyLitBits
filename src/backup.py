"""Nightly backup of the SQLite database.

Uses SQLite's online backup API, so it is safe while the app is serving, and
then opens the copy and runs an integrity check before calling it a success.

That verification is the point. The previous version shelled out to mongodump
without credentials and wrote 23-byte files containing nothing but an
authorisation error, every night for seven months, while reporting success.
A backup nobody has restored is just a file taking up space - the restore
command is in CLAUDE.md, and it is one line.

Off-box copy (2026-11-26): if BACKUP_OFFSITE is set in .env (an rclone path,
e.g. remote:folder), each verified backup is also uploaded there and copies
older than OFFSITE_RETENTION_DAYS are pruned. Until then every backup lived
only on the droplet it was protecting. Note the copy is useless without the
keys in .env (emails are encrypted with them), so keep .env somewhere safe too.
"""
import datetime
import glob
import gzip
import os
import shutil
import sqlite3
import subprocess
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
OFFSITE = os.getenv("BACKUP_OFFSITE", "").strip()
OFFSITE_RETENTION_DAYS = int(os.getenv("OFFSITE_RETENTION_DAYS", 30))


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
    return final_path


def rclone(*args):
    return subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=1800)


def upload_offsite(path):
    """Copy one verified backup off the box, then prune old copies there.
    Returns False on failure, so cron's exit status shows it."""
    if not OFFSITE:
        log("Off-box copy: BACKUP_OFFSITE not set, skipping.")
        return True
    log(f"Off-box copy: uploading {os.path.basename(path)}...")
    up = rclone("copy", path, OFFSITE)
    if up.returncode != 0:
        log(f"Error: off-box upload failed: {up.stderr.strip()[-500:]}")
        return False
    listed = rclone("lsf", OFFSITE, "--include", os.path.basename(path))
    if os.path.basename(path) not in listed.stdout:
        log("Error: off-box upload reported success but the file is not there.")
        return False
    prune = rclone("delete", OFFSITE, "--min-age", f"{OFFSITE_RETENTION_DAYS}d",
                   "--include", "dailylitbits_*.db.gz")
    if prune.returncode != 0:
        log(f"Warning: pruning old off-box copies failed: {prune.stderr.strip()[-300:]}")
    kept = rclone("lsf", OFFSITE, "--include", "dailylitbits_*.db.gz").stdout.split()
    log(f"Off-box copy: done, {len(kept)} kept (up to {OFFSITE_RETENTION_DAYS} days).")
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
    final_path = create_backup()
    if not final_path:
        sys.exit(1)
    cleanup_old_backups()
    if not upload_offsite(final_path):
        sys.exit(1)
