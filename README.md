# UpscaleHD – Free 1080p Video Upscaler

Upload a low-resolution video (240p, 360p, 480p, 720p, …), get a real **1920×1080** file back, and keep the
**original audio untouched** (stream-copied whenever the container allows it).

* **Fast Upscaling** – FFmpeg Lanczos resize + efficient H.264 encoding (GPU when available, CPU otherwise).
* **AI Enhancement (optional)** – Real-ESRGAN frame enhancement for sharper perceived detail. Needs a Vulkan GPU.

> Plain resizing makes a video 1080p but **cannot recreate detail that was never recorded**. AI enhancement can add
> plausible detail but is much slower and results depend on the footage. The UI says this too.

---

## 1. What works

| Area | Status |
|---|---|
| Drag-and-drop / browse upload, 5 GB limit enforced **server-side** | Tested |
| Chunked, resumable, streamed-to-disk upload (8 MB chunks, retries, cancel) | Tested (1.5 GiB streamed, memory stayed flat) |
| Real content validation with FFprobe (fake/corrupt/audio-only files rejected) | Tested |
| MP4 / MKV / MOV / AVI / WebM inputs | Tested |
| 240p / 360p / 480p / 720p → 1080p, 4:3 (never stretched), portrait, rotation metadata | Tested |
| Aspect modes: Preserve / Fit with padding / Crop to fill | Tested |
| Audio: bit-exact stream copy, multiple tracks, silent videos, incompatible-codec handling | Tested (audio packet hashes compared) |
| Original frame rate and variable-frame-rate timestamps preserved | Tested |
| Quality presets Fast / Balanced / High | Tested |
| Job queue, duplicate protection, live progress (SSE with polling fallback), ETA, cancel, cleanup | Tested |
| Download with HTTP Range support, preview that does not auto-download, delete-my-files, expiry | Tested |
| Per-browser isolation (one user cannot read another user's files) | Tested |
| UI in a real browser (upload → convert → download, desktop + mobile layout) | Tested with headless Chromium |
| NVIDIA NVENC / Intel Quick Sync / AMD AMF encoders | **Implemented, not tested** – no GPU on the dev machine. Detection runs a real test encode; failure falls back to CPU automatically. |
| AI Enhancement (Real-ESRGAN) | **Implemented, not tested end-to-end** – no Vulkan GPU / Real-ESRGAN binary on the dev machine. Availability is self-tested at runtime and the option is disabled with a reason if it fails. |
| Uploads near the full 5 GB | **Not tested with a real 5 GB file.** The code path is size-independent (chunked), but run your own large test. |

Automated tests: `31 passed` (pytest) plus the browser script in `scripts/e2e_browser.py`.

---

## 2. Requirements

* Python 3.10+ (3.12 tested)
* **FFmpeg + FFprobe** (a build that includes `libx264`; the "full" builds do)
* Optional: Real-ESRGAN ncnn-vulkan + a Vulkan-capable GPU (AI mode)
* Optional: NVIDIA / Intel / AMD GPU with current drivers (hardware encoding)

### Install FFmpeg on Windows

Option A – winget (PowerShell):

```powershell
winget install Gyan.FFmpeg
```

Close and reopen PowerShell, then check: `ffmpeg -version` and `ffprobe -version`.

Option B – manual: download a "full" build (e.g. from gyan.dev or BtbN), unzip to `C:\ffmpeg`, add
`C:\ffmpeg\bin` to your **PATH** (Start → "Edit environment variables"), reopen the terminal. Or skip PATH and set
`FFMPEG_BIN` / `FFPROBE_BIN` in `.env` to the full paths of the `.exe` files.

macOS: `brew install ffmpeg` · Ubuntu/Debian: `sudo apt install ffmpeg`

---

## 3. Run it locally (Windows PowerShell)

```powershell
cd upscaler
python -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
copy .env.example .env              # optional – every setting has a default
python app.py
```

Open <http://127.0.0.1:5000>.

macOS / Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Production-style server (also works on Windows):

```powershell
waitress-serve --listen=127.0.0.1:5000 --threads=16 wsgi:app
```

> **Run exactly one server process.** Jobs live in that process's memory (this keeps setup free of Redis).
> Use threads, not multiple worker processes. See "Scaling" below if you need more.

### Test a sample video

No sample file? Make a 10-second 360p one with FFmpeg:

```powershell
ffmpeg -f lavfi -i testsrc2=s=640x360:r=25:d=10 -f lavfi -i sine=frequency=440:d=10 -c:v libx264 -pix_fmt yuv420p -c:a aac sample_360p.mp4
```

Upload it, press **Convert to 1080p**, then download. Verify:

```powershell
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 sample_360p_1080p.mp4   # 1920,1080
```

Run the automated tests:

```powershell
pytest -q                          # backend, ~2 minutes
pip install playwright; playwright install chromium
python scripts/e2e_browser.py      # real browser flow
```

---

## 4. Optional: AI Enhancement (Real-ESRGAN)

1. Download the **realesrgan-ncnn-vulkan** release for your OS from the Real-ESRGAN GitHub releases page and unzip it.
2. In `.env`:
   ```
   REALESRGAN_BIN=C:\tools\realesrgan-ncnn-vulkan\realesrgan-ncnn-vulkan.exe
   REALESRGAN_MODELS_DIR=C:\tools\realesrgan-ncnn-vulkan\models
   REALESRGAN_MODEL=realesr-animevideov3
   ```
3. Restart. At startup the app runs a tiny real enhancement as a self-test. If it fails (no Vulkan GPU, old drivers)
   the **AI Enhancement** option is disabled and shows why.

How AI mode works: frames are extracted → enhanced with Real-ESRGAN → re-encoded to 1080p with the **original audio,
subtitles and chapters copied from the source file**. Costs and caveats:

* It needs a lot of **temporary disk space** (frames as images). The server checks free space first and refuses
  rather than crashing.
* It is **much slower** than Fast Upscaling (often slower than real time).
* Output is re-timed to a **constant frame rate** equal to the source's average rate.
* Limited to `AI_MAX_FRAMES` (default 30 000, ≈ 20 min at 25 fps).
* If AI fails, the UI offers **Retry with Fast Upscaling** (the upload is kept for this).
* `realesr-animevideov3` suits animation/video; photographic footage may prefer another model supported by the binary.

### What needs a GPU?

| Feature | GPU needed? |
|---|---|
| Fast Upscaling (x264) | No – CPU |
| Hardware encoding (NVENC / QSV / AMF) | Optional speed-up; auto-falls back to CPU |
| AI Enhancement | **Yes** – Vulkan-capable GPU |

---

## 5. Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `MAX_UPLOAD_GB` | 5 | Hard per-file limit (enforced on the server) |
| `CHUNK_MB` | 8 | Upload chunk size (also the maximum request body) |
| `RETENTION_HOURS` | 24 | Converted files are deleted after this long |
| `UPLOAD_STALE_HOURS` | 6 | Abandoned partial uploads are deleted after this long |
| `MIN_FREE_GB` | 1 | Refuse work if less free disk than this would remain |
| `MAX_CONCURRENT_JOBS` | 1 | Simultaneous conversions (raise on strong hardware) |
| `MAX_JOBS_PER_OWNER` / `MAX_QUEUE_LENGTH` | 2 / 20 | Fairness limits |
| `JOB_TIMEOUT_SECONDS` | 21600 | A job running longer is stopped |
| `DELETE_SOURCE_AFTER_SUCCESS` | true | Original upload deleted once the result is finalised (kept after failure/cancel so you can retry, until it expires) |
| `DATA_DIR` | `./data` | Uploads and results (never served statically) |
| `FFMPEG_BIN`, `FFPROBE_BIN` | `ffmpeg`, `ffprobe` | Paths if not on PATH |
| `REALESRGAN_*`, `AI_MAX_FRAMES` | see above | Optional AI mode |

---

## 6. How audio is preserved

* Every audio track is mapped and `-c:a copy` is used, so codec, bitrate, sample rate, channels and timing are unchanged.
  The result page reports whether the copy was verified.
* **MP4 cannot hold every audio codec** (e.g. Vorbis, Opus, DTS, TrueHD, PCM). When that happens the app
  **switches the output to MKV** and tells you, so the audio is still untouched. If you prefer MP4, tick
  *"re-encode to AAC instead"* – only the incompatible tracks are re-encoded.
* Subtitles: copied into MKV; converted to `mov_text` for MP4 when they are text-based. Image-based subtitles
  (PGS/VobSub) cannot go in MP4 and are left out with a notice – choose MKV to keep them.
* No audio track? You get a normal silent video, no error.
* Rotation metadata is applied (phone videos come out upright); portrait videos become 1080×1920.
* Sources that are within ~1% of 16:9 (e.g. 426×240) are scaled to exactly 1920×1080; other ratios keep their shape
  (4:3 → 1440×1080) unless you choose padding or crop.
* SD sources (BT.601) are converted and tagged BT.709 so colours don't shift on HD players. HDR sources are **not**
  tone-mapped.

---

## 7. Deployment

### Recommended: local-processing mode (no cloud limits)

Run it on your own computer (section 3). Nothing leaves your machine, there is no upload bandwidth cost, and the
5 GB limit is only a safety setting – raise `MAX_UPLOAD_GB` if you like. This is the most reliable way to handle
large videos for free.

### Self-hosted server (VPS / home server)

```bash
waitress-serve --listen=127.0.0.1:8000 --threads=16 wsgi:app
```

Behind nginx (note `proxy_buffering off` – required for live progress via Server-Sent Events):

```nginx
server {
    listen 80;
    server_name example.com;
    client_max_body_size 16m;          # only one 8 MB chunk per request – no need for 5g!
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

Use HTTPS in production and set `COOKIE_SECURE=true` so the session cookie that isolates users is only sent over HTTPS. Set a strong `SECRET_KEY`.
Chunked uploads mean **no single request ever approaches 5 GB**, so proxy body-size and timeout limits are not a problem
as long as chunks are small.

### Storage and bandwidth you must plan for

Per 5 GB source video, the server may hold at the same time: the upload (≤ 5 GB) + the converted file (roughly
duration × 6–12 Mbit/s) + (AI mode only) many GB of temporary frames. Allow **≥ 2.5× the source size** for Fast mode and
far more for AI mode. Bandwidth is source size in + result size out, per conversion.

### Free hosting – be realistic

"Free software" is not "unlimited free hosting". I did **not** look up any specific provider's current limits, so check
the provider's own documentation before deploying. In general, free tiers often have: small or ephemeral disks, little
RAM/CPU, request/process time limits or sleeping apps, limited bandwidth, and **no GPU** – so a 5 GB upload and a long
FFmpeg job may simply not work there. Do not advertise "5 GB free conversions" publicly unless your host really
supports it. If it doesn't, lower `MAX_UPLOAD_GB`, shorten `RETENTION_HOURS`, state the limits on the About page, or use
local-processing mode.

### Scaling

The queue is in-process (one server process, several threads). For multiple machines you would replace
`backend/jobs.py`'s queue with Redis + RQ/Celery and move `DATA_DIR` to shared storage. Not implemented, to keep the
default setup dependency-free.

---

## 8. Security notes

* Files are stored under random 128-bit ids in `DATA_DIR`, outside the static folder; ids are validated as hex, and
  user-supplied filenames are never used as paths.
* FFmpeg/FFprobe/Real-ESRGAN are always started with argument lists (no shell).
* Extension **and** real container/stream content are checked; size is enforced per upload on the server.
* Each browser gets a signed session id; uploads, jobs, previews and downloads are only reachable by their owner
  (tested). There are no accounts. Anyone who shares a browser profile shares access.
* Limits on active uploads, jobs per user, queue length, job run time and free disk.
* Temporary files are deleted on cancel/failure/expiry; restarting the server discards unfinished work.

Known limits: no per-IP rate limiting or CAPTCHA (put a reverse proxy/WAF in front of a public site), and nothing here
virus-scans files (FFmpeg only decodes them – keep FFmpeg updated, as decoders are a classic attack surface).

---

## 9. Project layout

```
app.py               Flask app, REST API, SSE, downloads
wsgi.py              production entry point
config.py            all settings (env / .env)
backend/
  media.py           ffprobe, encoder detection, filters, command building, error classification
  uploads.py         chunked resumable upload store
  jobs.py            queue, workers, FFmpeg runner, AI pipeline, progress
  cleanup.py         periodic expiry of files
frontend/
  templates/index.html
  static/css/style.css
  static/js/app.js
tests/               pytest suite (31 tests)
scripts/e2e_browser.py   real-browser end-to-end check
```

## 10. Known limitations

* Processing speed is entirely hardware-dependent; no conversion time is promised or shown before it can be measured.
* Output codec is H.264 (8-bit) for broad compatibility; 10-bit/HDR sources are converted to 8-bit SDR.
* One video stream is processed (the first non-cover-art stream); data/attachment streams are dropped.
* Progress for sources with unknown duration is shown as indeterminate.
* AI mode and GPU encoders are untested on real hardware (see section 1).
