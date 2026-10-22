import json
import subprocess
import time
from pathlib import Path

import pytest

from backend import media
from tests.helpers import audio_md5, convert, download, make_sample, upload, wait

pytestmark = pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.mark.parametrize("w,h", [(426, 240), (640, 360), (854, 480), (1280, 720)])
def test_resolutions_to_1080p_with_bit_exact_audio(client, samples, tmp_path, w, h):
    src = make_sample(samples / f"in_{h}.mp4", w, h)
    up = upload(client, src).json["upload"]
    assert up["info"]["video"]["height"] == h
    job = convert(client, up["id"], quality="fast")
    assert job["status"] == "finished", job["error"]
    out = download(client, job, tmp_path / "out.mp4")
    info = media.probe(out)
    assert (info["video"]["width"], info["video"]["height"]) == (1920, 1080)
    assert audio_md5(out) == audio_md5(src)                     # stream copy: identical audio packets
    assert job["result"]["audio"]["status"] == "copied"
    assert job["result"]["output"]["filename"].endswith("_1080p.mp4")


@pytest.mark.parametrize("ext,vcodec,acodec", [
    (".mkv", "libx264", "aac"), (".mov", "libx264", "aac"), (".avi", "mpeg4", "mp3"), (".webm", "libvpx", "libvorbis"),
])
def test_input_containers(client, samples, tmp_path, ext, vcodec, acodec):
    src = make_sample(samples / f"in_fmt{ext}", 640, 360, vcodec=vcodec, audio=acodec)
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast")
    assert job["status"] == "finished", job["error"]
    out = download(client, job, tmp_path / ("out." + job["result"]["container"]))
    assert (media.probe(out)["video"]["height"]) == 1080
    if job["result"]["audio"]["status"] == "copied":
        assert audio_md5(out) == audio_md5(src)


def test_vorbis_audio_switches_to_mkv_instead_of_reencoding(client, samples, tmp_path):
    src = make_sample(samples / "vorbis.webm", 640, 360, vcodec="libvpx", audio="libvorbis")
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast", format="mp4")
    assert job["status"] == "finished"
    assert job["result"]["container"] == "mkv"
    assert any("MKV" in n for n in job["notes"])
    out = download(client, job, tmp_path / "o.mkv")
    assert audio_md5(out) == audio_md5(src)


def test_allow_audio_reencode_keeps_mp4(client, samples):
    src = make_sample(samples / "vorbis2.webm", 640, 360, vcodec="libvpx", audio="libvorbis")
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast", format="mp4", allow_audio_reencode=True)
    assert job["result"]["container"] == "mp4"
    assert job["result"]["audio"]["status"] == "reencoded"


def test_silent_video(client, samples, tmp_path):
    src = make_sample(samples / "silent.mp4", 640, 360, audio=None)
    up = upload(client, src).json["upload"]
    assert up["info"]["audio"] == []
    job = convert(client, up["id"], quality="fast")
    assert job["status"] == "finished", job["error"]
    assert job["result"]["audio"]["status"] == "none"
    assert media.probe(download(client, job, tmp_path / "s.mp4"))["audio"] == []


def test_multiple_audio_tracks_preserved(client, samples, tmp_path):
    src = samples / "multi.mkv"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=3", "-f", "lavfi", "-i", "sine=frequency=880:d=3",
                    "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast", format="mkv")
    out = download(client, job, tmp_path / "m.mkv")
    assert len(media.probe(out)["audio"]) == 2


def test_aspect_modes_and_portrait(client, samples, tmp_path):
    src43 = make_sample(samples / "ar43.mp4", 640, 480)
    up = upload(client, src43).json["upload"]
    dims = {}
    for mode in ("preserve", "pad", "crop"):
        job = convert(client, up["id"], quality="fast", aspect=mode)
        assert job["status"] == "finished", job["error"]
        v = media.probe(download(client, job, tmp_path / f"{mode}.mp4"))["video"]
        dims[mode] = (v["width"], v["height"])
    assert dims["preserve"] == (1440, 1080)       # 4:3 is never stretched
    assert dims["pad"] == (1920, 1080)
    assert dims["crop"] == (1920, 1080)

    portrait = make_sample(samples / "portrait.mp4", 360, 640)
    up = upload(client, portrait).json["upload"]
    job = convert(client, up["id"], quality="fast")
    v = media.probe(download(client, job, tmp_path / "p.mp4"))["video"]
    assert (v["width"], v["height"]) == (1080, 1920)


def test_rotation_metadata_is_applied(client, samples, tmp_path):
    src = make_sample(samples / "rot.mp4", 640, 360, extra=["-display_rotation:v:0", "90"]) \
        if False else None
    # Create a landscape-encoded file flagged as rotated 90 degrees (typical phone video).
    src = samples / "rot.mp4"
    base = make_sample(samples / "rot_base.mp4", 640, 360)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-display_rotation", "90", "-i", str(base),
                    "-c", "copy", str(src)], check=True)
    up = upload(client, src).json["upload"]
    assert up["info"]["video"]["rotation"] in (90, 270)
    assert up["info"]["video"]["display_height"] == 640
    job = convert(client, up["id"], quality="fast")
    v = media.probe(download(client, job, tmp_path / "r.mp4"))["video"]
    assert (v["width"], v["height"]) == (1080, 1920) and v["rotation"] == 0


def test_frame_rate_preserved(client, samples, tmp_path):
    src = make_sample(samples / "fps30.mp4", 640, 360, fps=30)
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast")
    assert round(media.probe(download(client, job, tmp_path / "f.mp4"))["video"]["fps"], 2) == 30.0


def test_quality_presets_work(client, samples):
    src = make_sample(samples / "q.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    for q in ("fast", "balanced", "high"):
        assert convert(client, up["id"], quality=q)["status"] == "finished"


def test_range_download_and_preview(client, samples):
    src = make_sample(samples / "range.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast")
    r = client.get(f"/api/jobs/{job['id']}/download", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.data) == 100
    assert "attachment" in client.get(f"/api/jobs/{job['id']}/download").headers["Content-Disposition"]
    assert client.get(f"/api/jobs/{job['id']}/preview", headers={"Range": "bytes=0-9"}).status_code == 206


def test_progress_is_real_and_monotonic(client, samples):
    src = make_sample(samples / "prog.mp4", 1280, 720, seconds=8)
    up = upload(client, src).json["upload"]
    r = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"quality": "high"}})
    jid, seen = r.json["job"]["id"], []
    while True:
        j = client.get(f"/api/jobs/{jid}").json["job"]
        if j["progress"] is not None:
            seen.append(j["progress"])
        if j["status"] in ("finished", "failed"):
            break
        time.sleep(0.1)
    assert j["status"] == "finished" and seen[-1] == 100.0
    assert seen == sorted(seen)
    assert all(p < 100 for p in seen[:-1])                  # never "100%" before the file is finalised


def test_sse_stream_ends_with_finished(client, samples):
    src = make_sample(samples / "sse.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    jid = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"quality": "fast"}}).json["job"]["id"]
    body = client.get(f"/api/jobs/{jid}/events").get_data(as_text=True)
    events = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    assert events[-1]["status"] == "finished"


def test_duplicate_job_returns_existing(client, samples):
    src = make_sample(samples / "dup.mp4", 1280, 720, seconds=6)
    up = upload(client, src).json["upload"]
    a = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"quality": "high"}})
    b = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"quality": "high"}})
    assert b.json["duplicate"] is True and a.json["job"]["id"] == b.json["job"]["id"]
    c = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"quality": "fast"}})
    assert c.status_code == 409
    wait(client, a.json["job"]["id"])


def test_cancel_cleans_temp_files(client, samples):
    from config import settings as cfg
    src = make_sample(samples / "cancel.mp4", 1280, 720, seconds=40)
    up = upload(client, src).json["upload"]
    jid = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"quality": "high"}}).json["job"]["id"]
    time.sleep(1.0)
    client.post(f"/api/jobs/{jid}/cancel")
    job = wait(client, jid)
    assert job["status"] == "cancelled"
    assert not list((cfg.JOBS_DIR / jid).glob("converting.*"))
    assert not list((cfg.JOBS_DIR / jid).glob("output.*"))


def test_delete_removes_files(client, samples):
    from config import settings as cfg
    src = make_sample(samples / "del.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast")
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert not (cfg.JOBS_DIR / job["id"]).exists()
    assert not (cfg.UPLOAD_DIR / up["id"]).exists()
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 404


# ------------------------------------------------------------------ validation
def test_rejects_bad_extension_and_size(client):
    assert client.post("/api/uploads", json={"filename": "x.exe", "size": 10}).json["error"]["code"] == "unsupported_format"
    r = client.post("/api/uploads", json={"filename": "x.mp4", "size": 5 * 1024 ** 3 + 1})
    assert r.status_code == 413 and r.json["error"]["code"] == "too_large"
    assert client.post("/api/uploads", json={"filename": "x.mp4", "size": 0}).json["error"]["code"] == "empty_file"


def test_corrupted_and_fake_files_rejected(client, tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is definitely not a video" * 1000)
    r = upload(client, fake)
    assert r.status_code == 400 and r.json["error"]["code"] == "corrupted"
    audio_only = tmp_path / "a.mkv"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1", str(audio_only)], check=True)
    assert upload(client, audio_only).json["error"]["code"] == "unsupported_format"


def test_truncated_video_is_handled(client, samples):
    src = make_sample(samples / "trunc_src.mp4", 640, 360, seconds=4)
    broken = samples / "trunc.mp4"
    broken.write_bytes(src.read_bytes()[:len(src.read_bytes()) // 2])      # moov atom is at the end -> unreadable
    r = upload(client, broken)
    assert r.status_code == 400 and r.json["error"]["code"] == "corrupted"


def test_upload_resume_and_oversize_chunk(client, samples):
    src = make_sample(samples / "resume.mp4", 640, 360, seconds=4)
    data = src.read_bytes()
    up = client.post("/api/uploads", json={"filename": "resume.mp4", "size": len(data)}).json["upload"]
    half = len(data) // 2
    assert client.put(f"/api/uploads/{up['id']}/chunk?offset=0", data=data[:half]).json["received"] == half
    assert client.get(f"/api/uploads/{up['id']}").json["upload"]["received"] == half      # resume point
    bad = client.put(f"/api/uploads/{up['id']}/chunk?offset=0", data=data[:10])
    assert bad.status_code == 409
    assert client.put(f"/api/uploads/{up['id']}/chunk?offset={half}", data=data[half:]).json["received"] == len(data)
    assert client.post(f"/api/uploads/{up['id']}/complete").status_code == 200


def test_cannot_access_other_users_files(client, samples, app_factory=None):
    from app import create_app
    src = make_sample(samples / "priv.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    job = convert(client, up["id"], quality="fast")
    other = client.application.test_client()               # fresh cookie jar = different user
    assert other.get(f"/api/jobs/{job['id']}").status_code == 404
    assert other.get(f"/api/jobs/{job['id']}/download").status_code == 404
    assert other.get(f"/api/uploads/{up['id']}").status_code == 404
    assert other.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert client.get("/api/jobs/../../etc/passwd").status_code == 404


def test_ai_mode_reports_unavailable_without_realesrgan(client, samples):
    cfg = client.get("/api/config").json
    if cfg["ai"]["available"]:
        pytest.skip("Real-ESRGAN is installed here")
    assert cfg["ai"]["reason"]
    src = make_sample(samples / "ai.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    r = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"mode": "ai"}})
    assert r.status_code == 400 and r.json["error"]["code"] == "ai_unavailable"


def test_hardware_detection_falls_back_to_cpu(client):
    caps = {c["id"]: c for c in client.get("/api/config").json["encoders"]}
    assert caps["libx264"]["available"]
    for hw in ("h264_nvenc", "h264_qsv", "h264_amf"):
        assert caps[hw]["available"] or caps[hw]["reason"]
    assert media.resolve_encoder("auto") in caps


def test_unavailable_gpu_encoder_rejected(client, samples):
    caps = {c["id"]: c for c in client.get("/api/config").json["encoders"]}
    missing = [k for k in ("h264_nvenc", "h264_qsv", "h264_amf") if not caps[k]["available"]]
    if not missing:
        pytest.skip("all GPU encoders available")
    src = make_sample(samples / "gpu.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    r = client.post("/api/jobs", json={"upload_id": up["id"], "settings": {"encoder": missing[0]}})
    assert r.status_code == 400 and r.json["error"]["code"] == "gpu_unavailable"


def test_invalid_settings_rejected(client, samples):
    src = make_sample(samples / "inv.mp4", 426, 240)
    up = upload(client, src).json["upload"]
    for bad in ({"quality": "ultra"}, {"aspect": "stretch"}, {"format": "exe"}, {"mode": "magic"}):
        assert client.post("/api/jobs", json={"upload_id": up["id"], "settings": bad}).status_code == 400
