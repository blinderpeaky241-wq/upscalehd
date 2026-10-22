"""Background conversion jobs: an in-process queue, worker threads and real progress."""
import collections
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from config import settings as cfg
from . import media
from .errors import AppError
from .uploads import valid_id

log = logging.getLogger("upscaler.jobs")
TERMINAL = ("finished", "failed", "cancelled")
STAGE_LABELS = {
    "queued": "Waiting in queue", "analysing": "Preparing", "encoding": "Converting to 1080p",
    "extracting": "AI: extracting frames", "enhancing": "AI: enhancing frames",
    "assembling": "AI: encoding 1080p video and copying audio", "finalising": "Finalising output file",
    "done": "Finished", "failed": "Failed", "cancelled": "Cancelled",
}


class Cancelled(Exception):
    pass


class ProcessFailed(Exception):
    def __init__(self, code, message, tail=""):
        super().__init__(message)
        self.code, self.message, self.tail = code, message, tail


def _safe_stem(name):
    stem = re.sub(r"[^\w\-. ]+", "_", Path(name).stem, flags=re.UNICODE).strip(" ._") or "video"
    return stem[:80]


class Job:
    def __init__(self, owner, upload, source_path, settings):
        self.id = secrets.token_hex(16)
        self.owner = owner
        self.upload_id = upload["id"]
        self.source_path = Path(source_path)
        self.source_info = upload["info"]
        self.original_name = upload["filename"]
        self.settings = settings
        self.status = "queued"
        self.stage = "queued"
        self.progress = 0.0
        self.stage_progress = None
        self.speed = None
        self.eta = None
        self.notes = []
        self.error = None
        self.fallback_available = False
        self.created = time.time()
        self.started = None
        self.finished = None
        self.expires = None
        self.result = None
        self.output = None
        self.download_name = None
        self.encoder = None
        self.cancel_flag = threading.Event()
        self.timed_out = False
        self.proc = None
        self.dir = cfg.JOBS_DIR / self.id

    @property
    def key(self):
        blob = json.dumps([self.owner, self.upload_id, self.settings], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def snapshot(self, queue_position=None):
        return {
            "id": self.id, "status": self.status, "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "progress": None if self.progress is None else round(self.progress, 2),
            "stage_progress": self.stage_progress, "speed": self.speed, "eta_seconds": self.eta,
            "queue_position": queue_position, "notes": list(self.notes), "error": self.error,
            "fallback_available": self.fallback_available, "created_at": self.created,
            "started_at": self.started, "finished_at": self.finished, "expires_at": self.expires,
            "settings": self.settings, "upload_id": self.upload_id,
            "source": self.source_info, "result": self.result, "encoder": self.encoder,
        }


class JobManager:
    def __init__(self, uploads):
        self.uploads = uploads
        self.jobs = {}
        self.pending = collections.deque()
        self.cond = threading.Condition()
        self.shutdown = False
        cfg.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._restore()
        for n in range(max(1, cfg.MAX_CONCURRENT_JOBS)):
            threading.Thread(target=self._worker, name=f"job-worker-{n}", daemon=True).start()

    # ------------------------------------------------------------------ API
    def submit(self, owner, upload_id, settings):
        upload, path = self.uploads.source_for_job(upload_id, owner)
        job = Job(owner, upload, path, settings)
        with self.cond:
            for other in self.jobs.values():
                if other.status in ("queued", "running") and other.key == job.key:
                    return other, True                      # duplicate request: return the existing job
                if other.status in ("queued", "running") and other.upload_id == upload_id:
                    raise AppError("duplicate", "This video is already being processed. Cancel that job first.", 409)
            mine = sum(1 for j in self.jobs.values() if j.owner == owner and j.status in ("queued", "running"))
            if mine >= cfg.MAX_JOBS_PER_OWNER:
                raise AppError("too_many_jobs", "You already have the maximum number of active conversions.", 429)
            if len(self.pending) >= cfg.MAX_QUEUE_LENGTH:
                raise AppError("queue_full", "The server queue is full. Please try again in a few minutes.", 503)
            self.jobs[job.id] = job
            self.pending.append(job.id)
            self.cond.notify()
        return job, False

    def get(self, job_id, owner):
        job = self.jobs.get(job_id) if valid_id(job_id) else None
        if not job or job.owner != owner:
            raise AppError("not_found", "Job not found. It may have expired or been deleted.", 404)
        return job

    def snapshot(self, job):
        pos = None
        with self.cond:
            if job.status == "queued" and job.id in self.pending:
                pos = list(self.pending).index(job.id) + 1
        return job.snapshot(pos)

    def cancel(self, job_id, owner):
        job = self.get(job_id, owner)
        job.cancel_flag.set()
        with self.cond:
            if job.id in self.pending:
                self.pending.remove(job.id)
                self._mark(job, "cancelled", "cancelled")
        proc = job.proc
        if proc and proc.poll() is None:
            _terminate(proc)
        return job

    def delete(self, job_id, owner):
        job = self.get(job_id, owner)
        if job.status in ("queued", "running"):
            job.cancel_flag.set()
            with self.cond:
                if job.id in self.pending:
                    self.pending.remove(job.id)
            if job.proc and job.proc.poll() is None:
                _terminate(job.proc)
            for _ in range(100):                            # wait (max ~10s) for the worker to release files
                if job.status in TERMINAL:
                    break
                time.sleep(0.1)
        self.jobs.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)
        self.uploads.remove_internal(job.upload_id)

    def active_upload_ids(self):
        return {j.upload_id for j in self.jobs.values() if j.status in ("queued", "running")}

    def purge_expired(self):
        now = time.time()
        for job in list(self.jobs.values()):
            if job.status in TERMINAL and job.expires and job.expires < now:
                self.jobs.pop(job.id, None)
                shutil.rmtree(job.dir, ignore_errors=True)

    # ----------------------------------------------------------- persistence
    def _persist(self, job):
        if job.status != "finished":
            return
        meta = job.snapshot()
        meta.update({"owner": job.owner, "output": job.output and Path(job.output).name,
                     "download_name": job.download_name, "original_name": job.original_name})
        (job.dir / "job.json").write_text(json.dumps(meta), encoding="utf-8")

    def _restore(self):
        for d in cfg.JOBS_DIR.iterdir():
            try:
                meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
                assert valid_id(meta["id"]) and meta["id"] == d.name and (d / meta["output"]).is_file()
                assert meta["expires_at"] > time.time()
            except Exception:
                shutil.rmtree(d, ignore_errors=True)        # unfinished or expired work from a previous run
                continue
            job = Job.__new__(Job)
            job.__dict__.update({
                "id": meta["id"], "owner": meta["owner"], "upload_id": meta["upload_id"], "source_path": None,
                "source_info": meta["source"], "original_name": meta["original_name"], "settings": meta["settings"],
                "status": "finished", "stage": "done", "progress": 100.0, "stage_progress": None, "speed": None,
                "eta": None, "notes": meta["notes"], "error": None, "fallback_available": False,
                "created": meta["created_at"], "started": meta["started_at"], "finished": meta["finished_at"],
                "expires": meta["expires_at"], "result": meta["result"], "output": str(d / meta["output"]),
                "download_name": meta["download_name"], "encoder": meta.get("encoder"),
                "cancel_flag": threading.Event(), "timed_out": False, "proc": None, "dir": d,
            })
            self.jobs[job.id] = job

    # --------------------------------------------------------------- worker
    def _worker(self):
        while True:
            with self.cond:
                while not self.pending and not self.shutdown:
                    self.cond.wait()
                if self.shutdown:
                    return
                job = self.jobs.get(self.pending.popleft())
            if job and job.status == "queued":
                self._execute(job)

    def _mark(self, job, status, stage):
        job.status, job.stage = status, stage
        job.finished = time.time()
        job.eta = job.speed = None
        if status in TERMINAL:
            job.expires = job.finished + cfg.RETENTION_HOURS * 3600

    def _execute(self, job):
        job.status, job.stage, job.started = "running", "analysing", time.time()
        job.dir.mkdir(parents=True, exist_ok=True)
        timer = threading.Timer(cfg.JOB_TIMEOUT_SECONDS, self._on_timeout, args=(job,))
        timer.daemon = True
        timer.start()
        try:
            if job.cancel_flag.is_set():
                raise Cancelled()
            if job.settings["mode"] == "ai":
                self._run_ai(job)
            else:
                self._run_fast(job)
            job.progress = 100.0
            self._mark(job, "finished", "done")
            if cfg.DELETE_SOURCE_AFTER_SUCCESS:
                self.uploads.remove_internal(job.upload_id)
            self._persist(job)
        except Cancelled:
            self._clean_job_files(job)
            if job.timed_out:
                job.error = {"code": "timeout", "message": "The conversion exceeded the server time limit and was stopped."}
                self._mark(job, "failed", "failed")
            else:
                self._mark(job, "cancelled", "cancelled")
        except AppError as e:
            log.warning("job %s failed: %s %s", job.id, e.code, e.message)
            self._clean_job_files(job)
            job.error = {"code": e.code, "message": e.message}
            job.fallback_available = job.settings["mode"] == "ai"
            self._mark(job, "failed", "failed")
        except MemoryError:
            self._clean_job_files(job)
            job.error = {"code": "memory", "message": "The server ran out of memory while processing this video."}
            self._mark(job, "failed", "failed")
        except Exception:
            log.exception("job %s crashed", job.id)
            self._clean_job_files(job)
            job.error = {"code": "internal", "message": "An unexpected server error occurred. Please try again."}
            job.fallback_available = job.settings["mode"] == "ai"
            self._mark(job, "failed", "failed")
        finally:
            timer.cancel()
            shutil.rmtree(job.dir / "work", ignore_errors=True)
            job.proc = None
            self.uploads.touch(job.upload_id)

    def _on_timeout(self, job):
        job.timed_out = True
        job.cancel_flag.set()
        if job.proc and job.proc.poll() is None:
            _terminate(job.proc)

    @staticmethod
    def _clean_job_files(job):
        shutil.rmtree(job.dir / "work", ignore_errors=True)
        for p in job.dir.glob("converting.*"):
            p.unlink(missing_ok=True)

    # ------------------------------------------------------------ utilities
    @staticmethod
    def _ensure_space(needed):
        free = shutil.disk_usage(cfg.JOBS_DIR).free
        if free < needed + cfg.MIN_FREE_BYTES:
            raise AppError("disk_space",
                           f"Not enough free disk space on the server (need about {needed / 1024 ** 3:.1f} GB, "
                           f"{free / 1024 ** 3:.1f} GB free).", 507)

    def _run_process(self, job, cmd, duration, on_progress, label):
        """Run FFmpeg, parse `-progress` output, support cancel/timeout. Raises Cancelled / ProcessFailed."""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            raise ProcessFailed("processing_failed", f"Could not start {label}: {e}")
        job.proc = proc
        tail = collections.deque(maxlen=40)

        def drain():
            for line in proc.stderr:
                tail.append(line.rstrip())
        t = threading.Thread(target=drain, daemon=True)
        t.start()
        values = {}
        for line in proc.stdout:
            if job.cancel_flag.is_set():
                _terminate(proc)
                break
            if "=" not in line:
                continue
            k, v = line.strip().split("=", 1)
            values[k] = v
            if k == "progress":
                us = values.get("out_time_us") or values.get("out_time_ms")
                try:
                    seconds = max(0.0, int(us) / 1_000_000)
                except (TypeError, ValueError):
                    seconds = None
                speed = None
                try:
                    speed = float(values.get("speed", "").rstrip("x"))
                except ValueError:
                    pass
                on_progress(seconds, speed)
        proc.wait()
        t.join(timeout=2)
        if job.cancel_flag.is_set():
            raise Cancelled()
        if proc.returncode != 0:
            text = "\n".join(tail)
            code, msg = media.classify_error(text)
            raise ProcessFailed(code, msg, text)

    def _ffmpeg_progress(self, job, duration, base, span):
        def cb(seconds, speed):
            if not duration or seconds is None:
                job.progress = None if base == 0 else job.progress
                job.stage_progress = None
                job.speed = speed
                return
            frac = min(1.0, seconds / duration)
            job.stage_progress = round(frac * 100, 1)
            job.progress = base + span * frac
            job.speed = speed
            job.eta = (duration - seconds) / speed if speed and speed > 0.01 else None
        return cb

    # --------------------------------------------------------- fast pipeline
    def _run_fast(self, job):
        info, s = job.source_info, job.settings
        ffmpeg, _ = media.get_tools()
        plan = media.make_plan(info, s)
        job.notes += plan["notes"]
        self._ensure_space(media.estimate_output_bytes(info, s["quality"]) * 1.2)
        preferred = media.resolve_encoder(s.get("encoder", "auto"))
        attempts = [preferred] + (["libx264"] if preferred != "libx264" else [])
        ext = "." + plan["container"]
        tmp = job.dir / f"converting{ext}"
        job.stage, job.progress = "encoding", 0.0
        for i, enc in enumerate(attempts):
            job.encoder = enc
            cmd = media.build_command(ffmpeg, job.source_path, tmp, info, plan, s, enc)
            try:
                self._run_process(job, cmd, info["duration"], self._ffmpeg_progress(job, info["duration"], 0, 99), "FFmpeg")
                break
            except ProcessFailed as f:
                tmp.unlink(missing_ok=True)
                log.warning("encoder %s failed: %s", enc, f.tail[-400:])
                if i + 1 < len(attempts) and f.code in ("gpu_unavailable", "processing_failed"):
                    job.notes.append(f"The hardware encoder ({enc}) failed, so the conversion was retried on the CPU.")
                    job.progress = 0.0
                    continue
                raise AppError(f.code, f.message)
        self._finalise(job, tmp, plan)

    def _finalise(self, job, tmp, plan):
        job.stage, job.speed, job.eta = "finalising", None, None
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise AppError("processing_failed", "FFmpeg finished but did not produce an output file.")
        out_info = media.probe(tmp, "output", tmp.stat().st_size)
        if not out_info["video"]:
            raise AppError("processing_failed", "The converted file has no video stream.")
        final = job.dir / f"output.{plan['container']}"
        os.replace(tmp, final)                              # only now does the file count as finished
        out_info["filename"] = f"{_safe_stem(job.original_name)}_1080p.{plan['container']}"
        job.output = str(final)
        job.download_name = out_info["filename"]
        out_info["size_bytes"] = final.stat().st_size
        finished = time.time()
        job.result = {
            "output": out_info, "audio": media.audio_report(job.source_info, out_info, plan),
            "conversion_seconds": round(finished - job.started, 1), "container": plan["container"],
            "mode": job.settings["mode"], "encoder": job.encoder,
            "downloadable_until": finished + cfg.RETENTION_HOURS * 3600,
        }
        if plan["container"] == "mkv":
            job.result["preview_note"] = "MKV files may not play in the browser preview. Use the download button."

    # ----------------------------------------------------------- AI pipeline
    def _run_ai(self, job):
        cap = media.ai_capability()
        if not cap["available"]:
            raise AppError("ai_unavailable", "AI enhancement is not available on this server: " + cap["reason"])
        info, s = job.source_info, job.settings
        v = info["video"]
        ffmpeg, _ = media.get_tools()
        exe, models = media._realesrgan_paths()
        plan = media.make_plan(info, s)
        job.notes += plan["notes"]
        fps = v["fps"] or 25.0
        total = v["nb_frames"] or (int(math.ceil((info["duration"] or 0) * fps)) if info["duration"] else 0)
        if not total:
            raise AppError("ai_unavailable", "AI enhancement needs a known video duration. Use Fast Upscaling for this file.")
        if total > cfg.AI_MAX_FRAMES:
            raise AppError("ai_too_long", f"AI enhancement is limited to {cfg.AI_MAX_FRAMES} frames "
                           f"(about {cfg.AI_MAX_FRAMES / fps / 60:.0f} minutes at this frame rate). Use Fast Upscaling "
                           "or trim the video.")
        scale = media.realesrgan_scale(info)
        dw, dh = v["display_width"], v["display_height"]
        need = total * (dw * dh * 1.8 + dw * scale * dh * scale * 0.35) + media.estimate_output_bytes(info, s["quality"])
        self._ensure_space(need * 1.15)
        job.notes.append("AI enhancement re-times the video to a constant frame rate "
                         f"({fps:.3f} fps) while frames are processed. Audio is still copied from the original.")

        work = job.dir / "work"
        in_dir, out_dir = work / "in", work / "out"
        in_dir.mkdir(parents=True)
        out_dir.mkdir()

        # 1) extract frames (0 - 10 %)
        job.stage, job.progress = "extracting", 0.0
        try:
            self._run_process(job, media.extract_frames_command(ffmpeg, job.source_path, in_dir / "%08d.png", info),
                              info["duration"], self._ffmpeg_progress(job, info["duration"], 0, 10), "FFmpeg")
        except ProcessFailed as f:
            raise AppError(f.code, f.message)
        frames = sum(1 for _ in in_dir.iterdir())
        if frames == 0:
            raise AppError("processing_failed", "No frames could be extracted from this video.")

        # 2) Real-ESRGAN (10 - 90 %)
        job.stage, job.progress, job.speed, job.eta = "enhancing", 10.0, None, None
        self._run_realesrgan(job, realesrgan_command(exe, models, in_dir, out_dir, scale), out_dir, frames)
        shutil.rmtree(in_dir, ignore_errors=True)           # free disk as early as possible

        # 3) encode + copy audio (90 - 100 %)
        job.stage, job.progress = "assembling", 90.0
        ext = "." + plan["container"]
        tmp = job.dir / f"converting{ext}"
        preferred = media.resolve_encoder(s.get("encoder", "auto"))
        attempts = [preferred] + (["libx264"] if preferred != "libx264" else [])
        for i, enc in enumerate(attempts):
            job.encoder = enc
            cmd = media.build_command(ffmpeg, job.source_path, tmp, info, plan, s, enc,
                                      frames=(str(out_dir / "%08d.jpg"), fps))
            try:
                self._run_process(job, cmd, frames / fps, self._ffmpeg_progress(job, frames / fps, 90, 9), "FFmpeg")
                break
            except ProcessFailed as f:
                tmp.unlink(missing_ok=True)
                if i + 1 < len(attempts):
                    job.notes.append(f"The hardware encoder ({enc}) failed, so the final encode was retried on the CPU.")
                    continue
                raise AppError(f.code, f.message)
        self._finalise(job, tmp, plan)

    def _run_realesrgan(self, job, cmd, out_dir, total):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace")
        except OSError as e:
            raise AppError("ai_unavailable", f"Could not start Real-ESRGAN: {e}")
        job.proc = proc
        tail = collections.deque(maxlen=30)
        threading.Thread(target=lambda: [tail.append(l.rstrip()) for l in proc.stderr], daemon=True).start()
        started = time.time()
        while proc.poll() is None:
            if job.cancel_flag.is_set():
                _terminate(proc)
                raise Cancelled()
            done = sum(1 for e in os.scandir(out_dir))
            frac = min(1.0, done / total)
            job.stage_progress = round(frac * 100, 1)
            job.progress = 10 + 80 * frac
            elapsed = time.time() - started
            rate = done / elapsed if elapsed > 3 and done else None
            job.speed = None
            job.eta = (total - done) / rate if rate else None
            time.sleep(1.0)
        if job.cancel_flag.is_set():
            raise Cancelled()
        produced = sum(1 for _ in os.scandir(out_dir))
        if proc.returncode != 0 or produced < total:
            log.warning("realesrgan failed rc=%s produced=%s/%s: %s", proc.returncode, produced, total, "\n".join(tail))
            raise AppError("ai_failed", "AI enhancement failed (the GPU may be unavailable or out of memory). "
                           "You can retry with ordinary FFmpeg upscaling.")


def realesrgan_command(exe, models, in_dir, out_dir, scale):
    return media.realesrgan_cmd(exe, models, in_dir, out_dir, scale, fmt="jpg")


def _terminate(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError:
        pass
