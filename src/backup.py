from logger import log
import os
import datetime
import subprocess
import glob
import sys

# --- CONFIGURATION ---
# Where to save files on the Host
BACKUP_DIR = os.path.expanduser("~/backups")
# The name of the container running Mongo (from BlueHorseshoe stack)
CONTAINER_NAME = "mongo" 
# How many days to keep backups before deleting old ones
RETENTION_DAYS = 3

def create_backup():
    # 1. Ensure directory exists
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
        log(f"Created backup directory: {BACKUP_DIR}")

    # 2. Generate Filename
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    filename = f"dailylitbits_{timestamp}.archive.gz"
    filepath = os.path.join(BACKUP_DIR, filename)

    log(f"Starting backup: {filename}...")

    # 3. Run mongodump via Docker
    # We pipe stdout from the container directly to a file on the host
    # Command: docker exec mongo mongodump --archive --gzip
    try:
        with open(filepath, "wb") as f:
            # Note: We do NOT need a URI here because we are executing 
            # inside the container, so it defaults to localhost:27017 (which is correct inside)
            cmd = ["docker", "exec", CONTAINER_NAME, "mongodump", "--archive", "--gzip"]
            
            process = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
            
            if process.returncode != 0:
                log(f"Error creating backup: {process.stderr.decode()}")
                return False
            
        # Get file size
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        log(f"Success! Backup saved to {filepath} ({size_mb:.2f} MB)")
        return True

    except Exception as e:
        log(f"Exception during backup: {e}")
        return False

def cleanup_old_backups():
    log(f"Cleaning up backups older than {RETENTION_DAYS} days...")
    now = time.time()
    cutoff = now - (RETENTION_DAYS * 86400)
    
    # List all archive files in the dir
    files = glob.glob(os.path.join(BACKUP_DIR, "*.archive.gz"))
    
    count = 0
    for f in files:
        if os.path.getmtime(f) < cutoff:
            os.remove(f)
            log(f"Deleted old backup: {os.path.basename(f)}")
            count += 1
            
    if count == 0:
        log("No old backups to delete.")

import time # Imported here for the cleanup function logic

if __name__ == "__main__":
    if create_backup():
        cleanup_old_backups()
    else:
        sys.exit(1)
