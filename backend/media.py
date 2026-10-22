"""FFprobe / FFmpeg helpers: probing, capability detection, conversion planning.

Nothing here uses a shell. Every subprocess call receives an argument list.
"""
import functools
import json
import math
import re
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from config import settings as cfg
from .errors import AppError

# Demuxers we accept (ffprobe's `format_name` is a comma separated list).
ACCEPTED_FORMATS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi"}

# Audio codecs that can be stream-copied into each output container.
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "mp2"}
MP4_TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

HW_ENCODERS = [
    ("h264_nvenc", "NVIDIA NVENC"),
    ("h264_qsv", "Intel Quick Sync"),
    ("h264_amf", "AMD AMF"),
]

QUALITY_INDEX = {"fast": 0, "balanced": 1, "high": 2}
# Rough output bitrates (Mbit/s) used only for disk-space estimates.
EST_MBPS = {"fast": 6, "balanced": 8, "high": 12}


# --------------------------------------------------------------------------
# Tool discovery
# --------------------------------------------------------------------------
def _resolve(binary):
    return shutil.which(binary) or (binary if Path(binary).is_file() else None)


def get_tools():
    ffmpeg, ffprobe = _resolve(cfg.FFMPEG_BIN), _resolve(cfg.FFPROBE_BIN)
    if not ffmpeg or not ffprobe:
        raise AppError(
            "ffmpeg_missing",
            "FFmpeg/FFprobe was not found on the server. Install FFmpeg and make sure it is on PATH "
            "(or set FFMPEG_BIN / FFPROBE_BIN in .env).",
            503,
        )
    return ffmpeg, ffprobe


@functools.lru_cache(maxsize=1)
def ffmpeg_version():
    ffmpeg, _ = get_tools()
    out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=20,
                         encoding="utf-8", errors="replace").stdout
    m = re.search(r"ffmpeg version\s+n?(\d+)\.(\d+)", out)
    first = out.splitlines()[0] if out else "unknown"
    return (int(m.group(1)), int(m.group(2))) if m else (99, 0), first


def vfr_args():
    """Keep original timestamps (variable frame rate safe) on every FFmpeg version."""
    (major, minor), _ = ffmpeg_version()
    return ["-fps_mode", "passthrough"] if (major, minor) >= (5, 1) else ["-vsync", "0"]


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------
def probe(path, filename=None, size=None):
    _, ffprobe = get_tools()
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise AppError("corrupted", "The video could not be analysed (timed out). The file may be corrupted.")
    if r.returncode != 0 or not r.stdout.strip():
        raise AppError("corrupted", "This file could not be read as a valid video. It may be corrupted or not a video.")
    try:
        raw = json.loads(r.stdout)
    except ValueError:
        raise AppError("corrupted", "FFprobe returned unreadable metadata for this file.")
    return summarize(raw, filename or Path(path).name, size if size is not None else Path(path).stat().st_size)


def _fps(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if value and value != "0/0":
            try:
                f = Fraction(value)
                if f > 0:
                    return float(f)
            except (ValueError, ZeroDivisionError):
                pass
    return None


def _rotation(stream):
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    tag = (stream.get("tags") or {}).get("rotate")
    if tag:
        try:
            return int(tag) % 360
        except ValueError:
            pass
    return 0


def _sar(stream):
    value = stream.get("sample_aspect_ratio")
    if value and value not in ("0:1", "N/A", "1:1"):
        try:
            n, d = value.split(":")
            f = Fraction(int(n), int(d))
            if f > 0 and abs(float(f) - 1) > 0.005:
                return float(f)
        except (ValueError, ZeroDivisionError):
            pass
    return None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize(raw, filename, size):
    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []
    video = None
    for s in streams:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            video = s
            break
    info = {
        "filename": filename,
        "size_bytes": size,
        "container": fmt.get("format_name", ""),
        "duration": _num(fmt.get("duration")),
        "bit_rate": _num(fmt.get("bit_rate")),
        "video": None,
        "audio": [],
        "subtitles": [],
    }
    if video:
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        rot = _rotation(video)
        dw, dh = (h, w) if rot in (90, 270) else (w, h)
        avg, real = _num_frac(video.get("avg_frame_rate")), _num_frac(video.get("r_frame_rate"))
        vfr = bool(avg and real and abs(avg - real) / real > 0.02)
        info["video"] = {
            "index": video["index"], "codec": video.get("codec_name"), "width": w, "height": h,
            "display_width": dw, "display_height": dh, "rotation": rot, "fps": _fps(video), "vfr": vfr,
            "pix_fmt": video.get("pix_fmt"), "sar": _sar(video), "color_space": video.get("color_space") or "",
            "nb_frames": int(_num(video.get("nb_frames")) or 0) or None,
        }
        if info["duration"] is None:
            info["duration"] = _num(video.get("duration"))
    for s in streams:
        tags = s.get("tags") or {}
        if s.get("codec_type") == "audio":
            info["audio"].append({
                "index": s["index"], "codec": s.get("codec_name"), "profile": s.get("profile"),
                "sample_rate": int(_num(s.get("sample_rate")) or 0), "channels": int(s.get("channels") or 0),
                "channel_layout": s.get("channel_layout"), "bit_rate": _num(s.get("bit_rate")),
                "language": tags.get("language"),
            })
        elif s.get("codec_type") == "subtitle":
            info["subtitles"].append({"index": s["index"], "codec": s.get("codec_name"), "language": tags.get("language")})
    return info


def _num_frac(value):
    try:
        f = Fraction(value)
        return float(f) if f > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def validate_media(info):
    formats = set((info["container"] or "").split(","))
    if not formats & ACCEPTED_FORMATS:
        raise AppError("unsupported_format", "This file is not an MP4, MKV, MOV, AVI or WebM video "
                       f"(detected container: {info['container'] or 'unknown'}).")
    v = info["video"]
    if not v or not v["width"] or not v["height"]:
        raise AppError("unsupported_format", "No usable video stream was found in this file.")
    if not v["codec"]:
        raise AppError("unsupported_codec", "The video codec in this file is not supported by this server's FFmpeg build.")
    if max(v["width"], v["height"]) > 7680:
        raise AppError("unsupported_format", "Videos larger than 8K are not supported.")


# --------------------------------------------------------------------------
# Capability detection
# --------------------------------------------------------------------------
def _test_encode(ffmpeg, encoder):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25:d=0.4",
           "-pix_fmt", "yuv420p", "-c:v", encoder, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=40).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@functools.lru_cache(maxsize=1)
def encoder_capabilities():
    ffmpeg, _ = get_tools()
    listing = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30,
                             encoding="utf-8", errors="replace").stdout
    caps = []
    has_x264 = "libx264" in listing
    caps.append({"id": "libx264", "label": "CPU (x264)", "available": has_x264,
                 "reason": None if has_x264 else "This FFmpeg build has no libx264 encoder. Install a full FFmpeg build."})
    for name, label in HW_ENCODERS:
        if name not in listing:
            caps.append({"id": name, "label": label, "available": False,
                         "reason": "Not compiled into this FFmpeg build."})
        elif _test_encode(ffmpeg, name):
            caps.append({"id": name, "label": label, "available": True, "reason": None})
        else:
            caps.append({"id": name, "label": label, "available": False,
                         "reason": "Supported by FFmpeg, but no compatible GPU/driver was found (test encode failed)."})
    return caps


def resolve_encoder(preference):
    caps = {c["id"]: c for c in encoder_capabilities()}
    if preference in (None, "auto"):
        for name, _ in HW_ENCODERS:
            if caps[name]["available"]:
                return name
        preference = "cpu"
    if preference == "cpu":
        if not caps["libx264"]["available"]:
            raise AppError("encoder_unavailable", caps["libx264"]["reason"], 503)
        return "libx264"
    if preference not in caps or not caps[preference]["available"]:
        reason = caps.get(preference, {}).get("reason") or "Unknown encoder."
        raise AppError("gpu_unavailable", f"That hardware encoder is unavailable: {reason}")
    return preference


def _realesrgan_paths():
    exe = _resolve(cfg.REALESRGAN_BIN)
    if not exe:
        return None, None
    models = Path(cfg.REALESRGAN_MODELS_DIR) if cfg.REALESRGAN_MODELS_DIR else Path(exe).parent / "models"
    return exe, models


@functools.lru_cache(maxsize=1)
def ai_capability():
    exe, models = _realesrgan_paths()
    base = {"available": False, "model": cfg.REALESRGAN_MODEL, "reason": None}
    if not exe:
        base["reason"] = ("The Real-ESRGAN (ncnn-vulkan) executable was not found. Download it and set "
                          "REALESRGAN_BIN in .env (see README).")
        return base
    if not models.is_dir():
        base["reason"] = f"Real-ESRGAN models folder not found ({models}). Set REALESRGAN_MODELS_DIR in .env."
        return base
    ffmpeg, _ = get_tools()
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "t.png", Path(tmp) / "o.png"
        try:
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=64x64:d=0.1",
                            "-frames:v", "1", str(src)], capture_output=True, timeout=30, check=True)
            r = subprocess.run(realesrgan_cmd(exe, models, src, dst, 2), capture_output=True, timeout=120)
            ok = r.returncode == 0 and dst.is_file()
        except (subprocess.SubprocessError, OSError):
            ok = False
    if not ok:
        base["reason"] = ("Real-ESRGAN is installed but failed its self-test. It needs a Vulkan-capable GPU "
                          "with up-to-date drivers.")
        return base
    base["available"] = True
    return base


def realesrgan_cmd(exe, models, src, dst, scale, fmt=None):
    cmd = [exe, "-i", str(src), "-o", str(dst), "-n", cfg.REALESRGAN_MODEL, "-s", str(scale), "-m", str(models)]
    if fmt:
        cmd += ["-f", fmt]
    if cfg.REALESRGAN_GPU_ID:
        cmd += ["-g", cfg.REALESRGAN_GPU_ID]
    return cmd


def realesrgan_scale(info):
    """Model scale factor: smallest of 2..4 that reaches 1080p on the long side."""
    v = info["video"]
    long_src = max(v["display_width"], v["display_height"])
    needed = math.ceil(1920 / long_src)
    if cfg.REALESRGAN_MODEL.startswith("realesr-animevideov3"):
        return min(4, max(2, needed))
    return 4  # e.g. realesrgan-x4plus only ships a x4 model


# --------------------------------------------------------------------------
# Planning and command building
# --------------------------------------------------------------------------
def target_size(info):
    v = info["video"]
    return (1920, 1080) if v["display_width"] >= v["display_height"] else (1080, 1920)


def needs_color_fix(info):
    """SD sources are tagged BT.601; HD players assume BT.709, so convert to avoid a colour shift."""
    v = info["video"]
    return v["height"] < 720 and v["color_space"] in ("", "unknown", "smpte170m", "bt470bg", "smpte240m")


def build_video_filter(info, aspect, *, sar_fix=True, color_fix=False):
    tw, th = target_size(info)
    v = info["video"]
    parts = []
    if sar_fix and v["sar"]:
        parts += ["scale=trunc(iw*sar/2)*2:ih", "setsar=1"]
    cf = ":out_color_matrix=bt709" if color_fix else ""
    # Sources within ~1.2% of the target ratio (e.g. 426x240, 854x480) are scaled straight to the exact
    # target size; the distortion is invisible and it avoids odd 1918x1080 outputs.
    sar = v["sar"] or 1.0
    src_ratio = (v["display_width"] * sar) / v["display_height"]
    if aspect == "preserve" and abs(src_ratio / (tw / th) - 1) < 0.012:
        parts += [f"scale=w={tw}:h={th}:flags=lanczos{cf}", "setsar=1", "format=yuv420p"]
        return ",".join(parts)
    if aspect == "crop":
        parts += [f"scale=w={tw}:h={th}:force_original_aspect_ratio=increase:flags=lanczos{cf}", f"crop={tw}:{th}"]
    else:
        parts.append(f"scale=w={tw}:h={th}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos{cf}")
        if aspect == "pad":
            parts.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black")
    parts += ["setsar=1", "format=yuv420p"]
    return ",".join(parts)


def encoder_args(encoder, quality):
    q = QUALITY_INDEX[quality]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", ["veryfast", "medium", "slow"][q], "-crf", str([23, 20, 17][q]),
                "-profile:v", "high"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", ["p1", "p4", "p6"][q], "-rc", "vbr", "-cq", str([28, 24, 20][q]),
                "-b:v", "0", "-profile:v", "high"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", ["veryfast", "medium", "slower"][q],
                "-global_quality", str([27, 23, 19][q])]
    if encoder == "h264_amf":
        qp = str([26, 22, 18][q])
        return ["-c:v", "h264_amf", "-quality", ["speed", "balanced", "quality"][q], "-rc", "cqp",
                "-qp_i", qp, "-qp_p", qp]
    raise AppError("encoder_unavailable", f"Unknown encoder {encoder}.")


def make_plan(info, s):
    """Decide container, audio handling and subtitle handling. Returns a dict."""
    notes = []
    container = s["format"]
    audio = info["audio"]
    incompatible = [a for a in audio if (a["codec"] or "") not in MP4_AUDIO]
    reencode = set()
    if container == "mp4" and incompatible:
        names = ", ".join(sorted({(a["codec"] or "unknown").upper() for a in incompatible}))
        if s.get("allow_audio_reencode"):
            reencode = {a["index"] for a in incompatible}
            notes.append(f"MP4 cannot hold {names} audio, so those tracks were re-encoded to AAC (you allowed this).")
        else:
            container = "mkv"
            notes.append(f"MP4 cannot store {names} audio without re-encoding, so the output was switched to MKV "
                         "to keep your original audio untouched.")
    audio_plan = [{"index": a["index"], "action": "aac" if a["index"] in reencode else "copy",
                   "codec": a["codec"], "bit_rate": a["bit_rate"]} for a in audio]

    subs, dropped = [], []
    for sub in info["subtitles"]:
        if container == "mp4" and (sub["codec"] or "") not in MP4_TEXT_SUBS:
            dropped.append(sub["codec"] or "unknown")
        else:
            subs.append(sub["index"])
    if dropped:
        notes.append(f"{len(dropped)} image-based subtitle track(s) ({', '.join(sorted(set(dropped)))}) cannot be "
                     "stored in MP4 and were left out. Choose MKV to keep them.")
    if not audio:
        notes.append("The source has no audio track, so the output is a silent video.")
    v = info["video"]
    if v["vfr"]:
        notes.append("Variable frame rate detected; original timestamps were preserved.")
    dw, dh = v["display_width"], v["display_height"]
    if (dw >= 1920 and dh >= 1080) or (dh >= 1920 and dw >= 1080):
        notes.append("The source is already 1080p or larger, so it was resized rather than upscaled.")
    return {"container": container, "audio": audio_plan, "subs": subs, "notes": notes}


def build_command(ffmpeg, src, dst, info, plan, s, encoder, *, frames=None):
    """Build the final encode command.

    frames=None      -> read the video straight from `src`.
    frames=(pattern, fps) -> video comes from an upscaled image sequence; audio, subtitles,
                             chapters and metadata still come from `src`.
    """
    mp4 = plan["container"] == "mp4"
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    if frames:
        pattern, fps = frames
        cmd += ["-framerate", f"{fps:.6f}", "-i", pattern, "-i", str(src)]
        vmap, side = "0:v:0", "1"
    else:
        cmd += ["-i", str(src)]
        vmap, side = f"0:{info['video']['index']}", "0"
    cmd += ["-map", vmap]
    for a in plan["audio"]:
        cmd += ["-map", f"{side}:{a['index']}"]
    for idx in plan["subs"]:
        cmd += ["-map", f"{side}:{idx}"]
    cmd += ["-map_chapters", side, "-map_metadata", side]

    color_fix = needs_color_fix(info)
    vf = build_video_filter(info, s["aspect"], sar_fix=not frames, color_fix=color_fix)
    cmd += ["-vf", vf] + encoder_args(encoder, s["quality"]) + ["-pix_fmt", "yuv420p"]
    if color_fix:
        cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
    if not frames:
        cmd += vfr_args()

    if plan["audio"]:
        cmd += ["-c:a", "copy"]
        for j, a in enumerate(plan["audio"]):
            if a["action"] == "aac":
                kbps = int(min(384, max(128, (a["bit_rate"] or 192000) / 1000)))
                cmd += [f"-c:a:{j}", "aac", f"-b:a:{j}", f"{kbps}k"]
    if plan["subs"]:
        cmd += ["-c:s", "mov_text" if mp4 else "copy"]
    if mp4:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-max_muxing_queue_size", "4096", str(dst)]
    return cmd


def extract_frames_command(ffmpeg, src, out_pattern, info):
    v = info["video"]
    chain = []
    if v["sar"]:
        chain += ["scale=trunc(iw*sar/2)*2:ih", "setsar=1"]
    chain.append(f"fps={v['fps'] or 25:.6f}")
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            "-i", str(src), "-map", f"0:{v['index']}", "-vf", ",".join(chain), "-an", "-sn", str(out_pattern)]


# --------------------------------------------------------------------------
# Result verification and error classification
# --------------------------------------------------------------------------
def audio_report(src_info, out_info, plan):
    if not src_info["audio"]:
        return {"status": "none", "message": "The source had no audio, so the output is a silent video."}
    if len(out_info["audio"]) != len(src_info["audio"]):
        return {"status": "error", "message": "Warning: the number of audio tracks changed during conversion."}
    copied = 0
    for src_a, out_a, p in zip(src_info["audio"], out_info["audio"], plan["audio"]):
        if p["action"] == "copy" and (src_a["codec"], src_a["sample_rate"], src_a["channels"]) == \
                (out_a["codec"], out_a["sample_rate"], out_a["channels"]):
            copied += 1
    n = len(src_info["audio"])
    if copied == n:
        return {"status": "copied", "message": f"Original audio preserved: {n} track(s) stream-copied without re-encoding "
                                               "(same codec, sample rate and channels)."}
    if copied:
        return {"status": "partial", "message": f"{copied} of {n} audio tracks were copied untouched; the rest were "
                                                "re-encoded to AAC because the container required it."}
    return {"status": "reencoded", "message": "Audio had to be re-encoded to AAC because MP4 cannot hold the original codec."}


def classify_error(tail):
    t = tail.lower()
    if "no space left" in t:
        return "disk_space", "The server ran out of disk space during conversion."
    if "cannot allocate memory" in t or "out of memory" in t or "memoryerror" in t:
        return "memory", "The server ran out of memory while processing this video. Try the Fast preset or a smaller video."
    if any(k in t for k in ("invalid data found", "moov atom not found", "error while decoding", "corrupt")):
        return "corrupted", "The video appears to be corrupted and could not be fully decoded."
    if any(k in t for k in ("unknown decoder", "decoder (codec", "not supported", "unsupported codec")):
        return "unsupported_codec", "This video uses a codec that this server's FFmpeg build cannot decode."
    if any(k in t for k in ("nvenc", "cuda", "qsv", "amf", "device creation failed", "mfx")):
        return "gpu_unavailable", "The hardware encoder failed to start."
    return "processing_failed", "FFmpeg could not finish converting this video."


def estimate_output_bytes(info, quality):
    return int((info["duration"] or 0) * EST_MBPS[quality] * 125000)
