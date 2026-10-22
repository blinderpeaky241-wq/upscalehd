"""Drive the real UI in headless Chromium: upload -> convert -> preview -> download.

Usage:  python scripts/e2e_browser.py            (needs: pip install playwright && playwright install chromium)
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp(prefix="upscaler-e2e-"))
sample = tmp / "my clip.mp4"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=6", "-f", "lavfi",
                "-i", "sine=frequency=440:d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(sample)], check=True)
env = dict(os.environ, DATA_DIR=str(tmp / "data"), CHUNK_MB="1", PORT="5077", MIN_FREE_GB="0")
server = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:5077/healthz"); break
        except OSError:
            time.sleep(0.2)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.goto("http://127.0.0.1:5077/")
        page.wait_for_function("document.querySelector('#encoder-select').options.length > 1")
        page.screenshot(path=str(tmp / "01_home.png"))
        page.set_input_files("#file-input", str(sample))
        page.wait_for_selector("#setup:not([hidden])", timeout=30000)
        print("info:", page.inner_text("#info-grid").replace("\n", " | "))
        page.screenshot(path=str(tmp / "02_setup.png"), full_page=True)
        page.select_option("select[name=quality]", "fast")
        page.click("#convert-btn")
        page.wait_for_selector("#result:not([hidden])", timeout=60000)
        print("result:", page.inner_text("#result-grid").replace("\n", " | "))
        print("audio:", page.inner_text("#audio-status"))
        page.screenshot(path=str(tmp / "03_result.png"), full_page=True)
        with page.expect_download() as dl:
            page.click("#download")
        out = tmp / dl.value.suggested_filename
        dl.value.save_as(out)
        print("downloaded:", out.name, out.stat().st_size, "bytes")
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
        print("downloaded video dimensions:", probe)
        assert probe == "1920,1080"
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(tmp / "04_mobile.png"), full_page=True)
        print("console/page errors:", errors or "none")
        browser.close()
    print("screens in", tmp)
finally:
    server.terminate()
