import hashlib
import subprocess
import time
from pathlib import Path


def make_sample(path, w, h, *, audio="aac", seconds=3, fps=25, extra=None, vcodec="libx264"):
    """Generate a small test video with FFmpeg's lavfi sources (no external files needed)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:d={seconds}"]
    cmd += ["-c:v", vcodec, "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", audio]
    cmd += (extra or []) + [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return Path(path)


def upload(client, path):
    data = Path(path).read_bytes()
    r = client.post("/api/uploads", json={"filename": Path(path).name, "size": len(data)})
    assert r.status_code == 201, r.json
    up = r.json["upload"]
    offset = 0
    chunk = 1024 * 1024
    while offset < len(data):
        piece = data[offset:offset + chunk]
        r = client.put(f"/api/uploads/{up['id']}/chunk?offset={offset}", data=piece,
                       content_type="application/octet-stream")
        assert r.status_code == 200, r.json
        offset = r.json["received"]
    return client.post(f"/api/uploads/{up['id']}/complete")


def convert(client, upload_id, timeout=180, **settings):
    r = client.post("/api/jobs", json={"upload_id": upload_id, "settings": settings})
    assert r.status_code in (200, 201), r.json
    return wait(client, r.json["job"]["id"], timeout)


def wait(client, job_id, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json["job"]
        if job["status"] in ("finished", "failed", "cancelled"):
            return job
        time.sleep(0.3)
    raise AssertionError("job timed out")


def audio_md5(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def download(client, job, dest):
    r = client.get(f"/api/jobs/{job['id']}/download")
    assert r.status_code == 200
    Path(dest).write_bytes(r.data)
    return Path(dest)
