"""Flask application: REST API + Server-Sent Events + static frontend."""
import json
import logging
import secrets
import time

from flask import Flask, Response, jsonify, render_template, request, send_file, session

from backend import cleanup, jobs as jobs_mod, media, uploads as uploads_mod
from backend.errors import AppError
from config import settings as cfg

VALID = {"mode": ("fast", "ai"), "quality": ("fast", "balanced", "high"),
         "aspect": ("preserve", "pad", "crop"), "format": ("mp4", "mkv")}


def create_app():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = Flask(__name__, template_folder="frontend/templates", static_folder="frontend/static")
    app.config.update(
        SECRET_KEY=cfg.secret_key(),
        MAX_CONTENT_LENGTH=cfg.CHUNK_BYTES + 1024 * 1024,   # a single request is at most one chunk
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=cfg.COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=7 * 24 * 3600,
    )
    uploads = uploads_mod.UploadStore()
    jobs = jobs_mod.JobManager(uploads)
    cleanup.start(uploads, jobs)

    def owner():
        if "sid" not in session:
            session["sid"] = secrets.token_hex(16)
            session.permanent = True
        return session["sid"]

    @app.errorhandler(AppError)
    def on_app_error(e):
        return jsonify(error={"code": e.code, "message": e.message}), e.status

    @app.errorhandler(413)
    def on_too_large(_):
        return jsonify(error={"code": "too_large", "message": "The request was too large."}), 413

    @app.errorhandler(404)
    def on_404(_):
        return jsonify(error={"code": "not_found", "message": "Not found."}), 404

    @app.errorhandler(500)
    def on_500(_):
        return jsonify(error={"code": "internal", "message": "Unexpected server error."}), 500

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    @app.get("/api/config")
    def config():
        owner()
        try:
            ffmpeg_ok, version, err = True, media.ffmpeg_version()[1], None
            encoders = media.encoder_capabilities()
            ai = media.ai_capability()
        except AppError as e:
            ffmpeg_ok, version, err, encoders = False, None, e.message, []
            ai = {"available": False, "reason": "FFmpeg is not installed.", "model": cfg.REALESRGAN_MODEL}
        return jsonify(
            ffmpeg={"ok": ffmpeg_ok, "version": version, "error": err},
            encoders=encoders, ai=ai,
            limits={"max_upload_bytes": cfg.MAX_UPLOAD_BYTES, "chunk_bytes": cfg.CHUNK_BYTES,
                    "extensions": list(cfg.ALLOWED_EXTENSIONS), "retention_hours": cfg.RETENTION_HOURS,
                    "ai_max_frames": cfg.AI_MAX_FRAMES},
        )

    # ----------------------------------------------------------- uploads
    @app.post("/api/uploads")
    def upload_create():
        media.get_tools()
        body = request.get_json(silent=True) or {}
        return jsonify(upload=uploads.create(owner(), body.get("filename"), body.get("size"))), 201

    @app.get("/api/uploads/<uid>")
    def upload_status(uid):
        return jsonify(upload=uploads.status(uid, owner()))

    @app.put("/api/uploads/<uid>/chunk")
    def upload_chunk(uid):
        try:
            offset = int(request.args.get("offset", ""))
        except ValueError:
            raise AppError("bad_request", "Missing or invalid offset.")
        return jsonify(uploads.write_chunk(uid, owner(), offset, request.stream))

    @app.post("/api/uploads/<uid>/complete")
    def upload_complete(uid):
        return jsonify(upload=uploads.complete(uid, owner()))

    @app.delete("/api/uploads/<uid>")
    def upload_delete(uid):
        uploads.delete(uid, owner())
        return jsonify(ok=True)

    # -------------------------------------------------------------- jobs
    def parse_settings(raw):
        raw = raw or {}
        s = {}
        for key, allowed in VALID.items():
            value = raw.get(key, allowed[0] if key != "quality" else "balanced")
            if value not in allowed:
                raise AppError("bad_request", f"Invalid {key}: {value!r}.")
            s[key] = value
        enc = raw.get("encoder", "auto")
        if enc not in ("auto", "cpu", "h264_nvenc", "h264_qsv", "h264_amf"):
            raise AppError("bad_request", f"Invalid encoder: {enc!r}.")
        s["encoder"] = enc
        s["allow_audio_reencode"] = bool(raw.get("allow_audio_reencode", False))
        s["resolution"] = "1080p"
        if s["mode"] == "ai" and not media.ai_capability()["available"]:
            raise AppError("ai_unavailable", "AI enhancement is unavailable: " + (media.ai_capability()["reason"] or ""))
        if enc not in ("auto", "cpu"):
            media.resolve_encoder(enc)       # raises a clear error if that GPU encoder is not usable
        return s

    @app.post("/api/jobs")
    def job_create():
        media.get_tools()
        body = request.get_json(silent=True) or {}
        job, duplicate = jobs.submit(owner(), body.get("upload_id"), parse_settings(body.get("settings")))
        return jsonify(job=jobs.snapshot(job), duplicate=duplicate), 200 if duplicate else 201

    @app.get("/api/jobs/<jid>")
    def job_get(jid):
        return jsonify(job=jobs.snapshot(jobs.get(jid, owner())))

    @app.get("/api/jobs/<jid>/events")
    def job_events(jid):
        job = jobs.get(jid, owner())

        def stream():
            last, idle = None, 0
            while True:
                snap = jobs.snapshot(job)
                payload = json.dumps(snap)
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last, idle = payload, 0
                else:
                    idle += 1
                    if idle % 30 == 0:
                        yield ": keep-alive\n\n"
                if snap["status"] in jobs_mod.TERMINAL:
                    return
                time.sleep(0.5)
        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/jobs/<jid>/cancel")
    def job_cancel(jid):
        job = jobs.cancel(jid, owner())
        return jsonify(job=jobs.snapshot(job))

    @app.delete("/api/jobs/<jid>")
    def job_delete(jid):
        jobs.delete(jid, owner())
        return jsonify(ok=True)

    def finished_job(jid):
        job = jobs.get(jid, owner())
        if job.status != "finished" or not job.output:
            raise AppError("not_ready", "The converted video is not ready.", 409)
        if job.expires and job.expires < time.time():
            raise AppError("expired", "This download has expired. Please convert the video again.", 410)
        return job

    @app.get("/api/jobs/<jid>/download")
    def job_download(jid):
        job = finished_job(jid)
        # conditional=True enables HTTP Range requests, so interrupted downloads can resume.
        return send_file(job.output, as_attachment=True, download_name=job.download_name, conditional=True,
                         max_age=0)

    @app.get("/api/jobs/<jid>/preview")
    def job_preview(jid):
        job = finished_job(jid)
        return send_file(job.output, as_attachment=False, download_name=job.download_name, conditional=True,
                         max_age=0)

    return app


if __name__ == "__main__":
    create_app().run(host=cfg.HOST, port=cfg.PORT, threaded=True, debug=False)
