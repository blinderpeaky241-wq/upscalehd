"""Periodic removal of expired uploads and converted files."""
import logging
import threading
import time

from config import settings as cfg

log = logging.getLogger("upscaler.cleanup")


def start(uploads, jobs):
    def loop():
        while True:
            time.sleep(cfg.CLEANUP_INTERVAL_SECONDS)
            try:
                jobs.purge_expired()
                uploads.purge_stale(protected_ids=jobs.active_upload_ids())
            except Exception:
                log.exception("cleanup failed")
    threading.Thread(target=loop, name="cleanup", daemon=True).start()
