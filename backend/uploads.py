"""Chunked, resumable uploads written straight to disk (never held in memory)."""
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from config import settings as cfg
from . import media
from .errors import AppError

ID_RE = re.compile(r"^[a-f0-9]{32}$")


def valid_id(value):
    return bool(value) and bool(ID_RE.match(value))


class UploadStore:
    def __init__(self):
        cfg.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.items = {}
        self._load()

    # -- persistence --------------------------------------------------------
    def _dir(self, uid):
        return cfg.UPLOAD_DIR / uid

    def _save(self, item):
        meta = {k: item[k] for k in ("id", "owner", "filename", "size", "created", "touched", "info", "state")}
        tmp = self._dir(item["id"]) / "meta.json.tmp"
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, self._dir(item["id"]) / "meta.json")

    def _load(self):
        for d in cfg.UPLOAD_DIR.iterdir():
            meta_file = d / "meta.json"
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                assert valid_id(meta["id"]) and meta["id"] == d.name
            except Exception:
                shutil.rmtree(d, ignore_errors=True)
                continue
            meta["lock"] = threading.Lock()
            self.items[meta["id"]] = meta

    # -- helpers ------------------------------------------------------------
    def _get(self, uid, owner):
        item = self.items.get(uid) if valid_id(uid) else None
        if not item or item["owner"] != owner:
            raise AppError("not_found", "Upload not found. It may have expired or been deleted.", 404)
        return item

    def path(self, item):
        return self._dir(item["id"]) / ("source" + Path(item["filename"]).suffix.lower())

    def received(self, item):
        p = self.path(item)
        return p.stat().st_size if p.exists() else 0

    def public(self, item):
        return {"id": item["id"], "filename": item["filename"], "size": item["size"], "state": item["state"],
                "received": self.received(item), "info": item["info"]}

    # -- API ----------------------------------------------------------------
    def create(self, owner, filename, size):
        filename = (filename or "").strip()
        ext = Path(filename).suffix.lower()
        if ext not in cfg.ALLOWED_EXTENSIONS:
            raise AppError("unsupported_format", "Unsupported file type. Allowed: " +
                           ", ".join(e.upper().lstrip(".") for e in cfg.ALLOWED_EXTENSIONS) + ".")
        if not isinstance(size, int) or size <= 0:
            raise AppError("empty_file", "The selected file is empty.")
        if size > cfg.MAX_UPLOAD_BYTES:
            raise AppError("too_large", f"File is larger than the {cfg.MAX_UPLOAD_BYTES / 1024 ** 3:g} GB limit.", 413)
        with self.lock:
            active = [i for i in self.items.values() if i["owner"] == owner and i["state"] == "uploading"]
            if len(active) >= cfg.MAX_ACTIVE_UPLOADS_PER_OWNER:
                raise AppError("too_many_uploads", "Too many uploads in progress. Finish or cancel one first.", 429)
            cfg.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(cfg.UPLOAD_DIR).free
            reserved = sum(i["size"] - self.received(i) for i in self.items.values() if i["state"] == "uploading")
            if free - reserved < size + cfg.MIN_FREE_BYTES:
                raise AppError("disk_space", "The server does not have enough free disk space for this upload.", 507)
            uid = secrets.token_hex(16)
            self._dir(uid).mkdir(parents=True)
            now = time.time()
            item = {"id": uid, "owner": owner, "filename": filename[:255], "size": size, "created": now,
                    "touched": now, "info": None, "state": "uploading", "lock": threading.Lock()}
            self.items[uid] = item
            self.path(item).touch()
            self._save(item)
        return self.public(item)

    def status(self, uid, owner):
        return self.public(self._get(uid, owner))

    def write_chunk(self, uid, owner, offset, stream):
        item = self._get(uid, owner)
        if item["state"] != "uploading":
            raise AppError("bad_state", "This upload is already complete.", 409)
        with item["lock"]:
            path = self.path(item)
            current = self.received(item)
            if offset != current:
                raise AppError("offset_mismatch", f"Expected offset {current}.", 409)
            remaining = item["size"] - current
            written = 0
            with open(path, "ab") as fh:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > remaining:
                        fh.truncate(current)
                        raise AppError("too_large", "Upload is larger than the size that was declared.", 413)
                    fh.write(block)
            item["touched"] = time.time()
            return {"received": current + written}

    def complete(self, uid, owner):
        item = self._get(uid, owner)
        with item["lock"]:
            if item["state"] == "ready":
                return self.public(item)
            if self.received(item) != item["size"]:
                raise AppError("incomplete", f"Upload incomplete ({self.received(item)} of {item['size']} bytes).", 409)
            try:
                info = media.probe(self.path(item), item["filename"], item["size"])
                media.validate_media(info)
            except AppError:
                self._remove(uid)
                raise
            item["info"], item["state"], item["touched"] = info, "ready", time.time()
            self._save(item)
        return self.public(item)

    def source_for_job(self, uid, owner):
        item = self._get(uid, owner)
        if item["state"] != "ready":
            raise AppError("bad_state", "Upload is not finished yet.", 409)
        return item, self.path(item)

    def touch(self, uid):
        item = self.items.get(uid)
        if item:
            item["touched"] = time.time()

    def _remove(self, uid):
        with self.lock:
            self.items.pop(uid, None)
        shutil.rmtree(self._dir(uid), ignore_errors=True)

    def delete(self, uid, owner):
        self._get(uid, owner)
        self._remove(uid)

    def remove_internal(self, uid):
        self._remove(uid)

    def purge_stale(self, protected_ids=()):
        cutoff_up = time.time() - cfg.UPLOAD_STALE_HOURS * 3600
        cutoff_ready = time.time() - cfg.RETENTION_HOURS * 3600
        for uid, item in list(self.items.items()):
            if uid in protected_ids:
                continue
            limit = cutoff_up if item["state"] == "uploading" else cutoff_ready
            if item["touched"] < limit:
                self._remove(uid)
