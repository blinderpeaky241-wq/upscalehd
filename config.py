"""Central configuration.

Every value can be overridden with an environment variable or a `.env` file
in the project root (see .env.example).
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv():
    path = BASE_DIR / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _str(name, default):
    return os.environ.get(name, str(default))


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return int(default)


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # --- server -----------------------------------------------------------
    HOST = _str("HOST", "127.0.0.1")
    PORT = _int("PORT", 5000)

    # --- storage (must NOT be inside the static folder) --------------------
    DATA_DIR = Path(_str("DATA_DIR", BASE_DIR / "data")).resolve()
    UPLOAD_DIR = DATA_DIR / "uploads"
    JOBS_DIR = DATA_DIR / "jobs"
    RETENTION_HOURS = _float("RETENTION_HOURS", 24)          # converted files
    UPLOAD_STALE_HOURS = _float("UPLOAD_STALE_HOURS", 6)     # abandoned uploads
    CLEANUP_INTERVAL_SECONDS = _int("CLEANUP_INTERVAL_SECONDS", 300)
    MIN_FREE_BYTES = int(_float("MIN_FREE_GB", 1) * 1024 ** 3)
    DELETE_SOURCE_AFTER_SUCCESS = _bool("DELETE_SOURCE_AFTER_SUCCESS", True)

    # --- uploads -----------------------------------------------------------
    MAX_UPLOAD_BYTES = int(_float("MAX_UPLOAD_GB", 5) * 1024 ** 3)
    CHUNK_BYTES = int(_float("CHUNK_MB", 8) * 1024 * 1024)
    ALLOWED_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
    MAX_ACTIVE_UPLOADS_PER_OWNER = _int("MAX_ACTIVE_UPLOADS_PER_OWNER", 3)

    # --- processing --------------------------------------------------------
    FFMPEG_BIN = _str("FFMPEG_BIN", "ffmpeg")
    FFPROBE_BIN = _str("FFPROBE_BIN", "ffprobe")
    MAX_CONCURRENT_JOBS = _int("MAX_CONCURRENT_JOBS", 1)
    MAX_QUEUE_LENGTH = _int("MAX_QUEUE_LENGTH", 20)
    MAX_JOBS_PER_OWNER = _int("MAX_JOBS_PER_OWNER", 2)       # queued + running
    JOB_TIMEOUT_SECONDS = _int("JOB_TIMEOUT_SECONDS", 6 * 3600)

    # --- optional AI enhancement (Real-ESRGAN ncnn-vulkan) ------------------
    REALESRGAN_BIN = _str("REALESRGAN_BIN", "realesrgan-ncnn-vulkan")
    REALESRGAN_MODEL = _str("REALESRGAN_MODEL", "realesr-animevideov3")
    REALESRGAN_MODELS_DIR = _str("REALESRGAN_MODELS_DIR", "")
    REALESRGAN_GPU_ID = _str("REALESRGAN_GPU_ID", "")
    AI_MAX_FRAMES = _int("AI_MAX_FRAMES", 30000)

    # --- security ----------------------------------------------------------
    COOKIE_SECURE = _bool("COOKIE_SECURE", False)   # set true when served over HTTPS
    @staticmethod
    def secret_key():
        env = os.environ.get("SECRET_KEY")
        if env:
            return env
        Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        key_file = Config.DATA_DIR / ".secret_key"
        if key_file.is_file():
            return key_file.read_text().strip()
        key = secrets.token_hex(32)
        key_file.write_text(key)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        return key


settings = Config
