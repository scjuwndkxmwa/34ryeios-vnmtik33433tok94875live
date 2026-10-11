
import os
import sys
import json
import time
import shutil
import signal
import subprocess
import threading
from pathlib import Path

PLAYLIST_URL = (
    "https://youtube.com/playlist?"
    "list=PLbB-zzxmyt-qxqhYuwRWiDCAFgH8bDgkR"
)

RESTREAM_KEY = "re_12012590_event263b321ab25446e5b8f3f975dabd2c6a"
RESTREAM_SERVER = "rtmp://live.restream.io/live"

VIDEO_FILE = Path("/app/loop.mp4")
AUDIO_FIFO = Path("/tmp/playlist_audio.pcm")

SAMPLE_RATE = 48000
CHANNELS = 2
CHUNK_SIZE = 65536

stop_event = threading.Event()
children = []


def log(message):
    print(message, flush=True)


def run_capture(command):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout


def get_playlist():
    log("[PLAYLIST] Reading playlist...")
    raw = run_capture([
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        PLAYLIST_URL,
    ])
    data = json.loads(raw)

    videos = []
    for entry in data.get("entries") or []:
        if entry and entry.get("id"):
            videos.append(
                f"https://www.youtube.com/watch?v={entry['id']}"
            )

    if not videos:
        raise RuntimeError("Playlist is empty or unavailable.")

    log(f"[PLAYLIST] Found {len(videos)} tracks.")
    return videos


def stream_audio(video_urls):
    with AUDIO_FIFO.open("wb", buffering=0) as audio_pipe:
        index = 0

        while not stop_event.is_set():
            url = video_urls[index]
            track_number = index + 1
            index = (index + 1) % len(video_urls)

            log(f"[AUDIO] Track {track_number}/{len(video_urls)}")

            downloader = subprocess.Popen([
                "yt-dlp",
                "--no-warnings",
                "--no-playlist",
                "-f", "bestaudio/best",
                "-o", "-",
                url,
            ], stdout=subprocess.PIPE, bufsize=0)
            children.append(downloader)

            converter = subprocess.Popen([
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-i", "pipe:0",
                "-vn",
                "-f", "s16le",
                "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS),
                "pipe:1",
            ], stdin=downloader.stdout,
               stdout=subprocess.PIPE, bufsize=0)
            children.append(converter)

            if downloader.stdout:
                downloader.stdout.close()

            try:
                while not stop_event.is_set():
                    chunk = converter.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    audio_pipe.write(chunk)
            finally:
                for proc in (converter, downloader):
                    if proc.poll() is None:
                        proc.terminate()
                for proc in (converter, downloader):
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    if proc in children:
                        children.remove(proc)

            if not stop_event.is_set():
                log("[AUDIO] Moving to next track.")
                time.sleep(0.3)


def stop_all(*_):
    stop_event.set()
    for proc in list(children):
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass


def main():
    if not VIDEO_FILE.is_file():
        raise FileNotFoundError("Upload loop.mp4 beside main.py.")

    for tool in ("ffmpeg", "yt-dlp"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing tool: {tool}")

    if AUDIO_FIFO.exists():
        AUDIO_FIFO.unlink()
    os.mkfifo(AUDIO_FIFO)

    videos = get_playlist()
    output_url = f"{RESTREAM_SERVER}/{RESTREAM_KEY}"

    log("[LIVE] Starting stream to Restream.")
    log("[VIDEO] loop.mp4 loops continuously.")
    log("[STORAGE] Audio is streamed without saving playlist files.")

    publisher = subprocess.Popen([
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-nostdin",
        "-stream_loop", "-1",
        "-re", "-i", str(VIDEO_FILE),
        "-re",
        "-f", "s16le",
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        "-i", str(AUDIO_FIFO),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,format=yuv420p",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-g", "60",
        "-b:v", "2200k",
        "-maxrate", "2200k",
        "-bufsize", "4400k",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-f", "flv",
        output_url,
    ])
    children.append(publisher)

    producer = threading.Thread(
        target=stream_audio,
        args=(videos,),
        daemon=True,
    )
    producer.start()

    try:
        while not stop_event.is_set():
            if publisher.poll() is not None:
                raise RuntimeError(
                    f"FFmpeg exited with code {publisher.returncode}"
                )
            time.sleep(2)
    finally:
        stop_all()
        if publisher.poll() is None:
            publisher.terminate()
            try:
                publisher.wait(timeout=10)
            except subprocess.TimeoutExpired:
                publisher.kill()
        if AUDIO_FIFO.exists():
            AUDIO_FIFO.unlink()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    try:
        main()
    except Exception as exc:
        log(f"[ERROR] {exc}")
        sys.exit(1)
