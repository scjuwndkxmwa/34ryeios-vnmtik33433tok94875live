import os
import sys
import json
import time
import base64
import signal
import shutil
import tempfile
import subprocess
from pathlib import Path

# ================= SETTINGS =================

RESTREAM_SERVER = "rtmp://live.restream.io/live"
RESTREAM_KEY = "re_12012590_event8d4fbd2ce937417fbfc8e0d48df82cdf"

PLAYLIST_URL = (
    "https://youtube.com/playlist?"
    "list=PLbB-zzxmyt-qxqhYuwRWiDCAFgH8bDgkR"
)

VIDEO_FILE = Path(os.getenv("VIDEO_FILE", "/app/loop.mp4"))
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()

VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "4500k")
VIDEO_MAXRATE = os.getenv("VIDEO_MAXRATE", "5000k")
VIDEO_BUFSIZE = os.getenv("VIDEO_BUFSIZE", "9000k")
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "veryfast")
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "30"))

AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "128k")
AUDIO_RATE = 44100
AUDIO_CHANNELS = 2
RETRY_DELAY = 10

stop_requested = False
cookies_path = None
main_ffmpeg = None


def log(message):
    print(message, flush=True)


def check_setup():
    for program in ("ffmpeg", "yt-dlp"):
        if not shutil.which(program):
            raise RuntimeError(f"Required program missing: {program}")

    if not RESTREAM_KEY:
        raise RuntimeError("RESTREAM_KEY is empty.")

    if not VIDEO_FILE.is_file():
        raise RuntimeError(f"Video file not found: {VIDEO_FILE}")


def make_cookie_file():
    global cookies_path

    if not YOUTUBE_COOKIES_B64:
        log("[COOKIES] Cookies are OFF.")
        return

    try:
        data = base64.b64decode(YOUTUBE_COOKIES_B64, validate=True)
        fd, path = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")

        with os.fdopen(fd, "wb") as file_obj:
            file_obj.write(data)

        cookies_path = path
        log("[COOKIES] Temporary cookie file loaded.")

    except Exception as exc:
        log(f"[COOKIES] Could not decode cookies: {exc}")


def yt_dlp_base_args():
    args = [
        "yt-dlp",
        "--no-warnings",
        "--extractor-args",
        "youtube:player_client=web_embedded,web,tv",
    ]

    if cookies_path:
        args.extend(["--cookies", cookies_path])

    return args


def get_playlist_tracks():
    log("[PLAYLIST] Reading playlist...")

    command = yt_dlp_base_args() + [
        "--flat-playlist",
        "--dump-single-json",
        PLAYLIST_URL,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "Playlist extraction failed:\n"
            + (result.stderr or "")[-3000:]
        )

    data = json.loads(result.stdout)
    tracks = []

    for entry in data.get("entries") or []:
        if not entry:
            continue

        video_id = entry.get("id")
        url = entry.get("webpage_url") or entry.get("original_url")

        if not url and video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"

        if url:
            tracks.append({
                "url": url,
                "title": entry.get("title") or video_id or "Untitled",
            })

    if not tracks:
        raise RuntimeError("No tracks found in playlist.")

    log(f"[PLAYLIST] Found {len(tracks)} tracks.")
    return tracks


def build_ffmpeg_command():
    target = f"{RESTREAM_SERVER.rstrip('/')}/{RESTREAM_KEY}"

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",

        # Video loops continuously
        "-stream_loop", "-1",
        "-re",
        "-i", str(VIDEO_FILE),

        # Incoming audio is raw PCM through stdin
        "-f", "s16le",
        "-ar", str(AUDIO_RATE),
        "-ac", str(AUDIO_CHANNELS),
        "-i", "pipe:0",

        "-map", "0:v:0",
        "-map", "1:a:0",

        # H.264 output for RTMP compatibility
        "-c:v", "libx264",
        "-preset", VIDEO_PRESET,
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-g", str(VIDEO_FPS * 2),
        "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_MAXRATE,
        "-bufsize", VIDEO_BUFSIZE,

        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ar", str(AUDIO_RATE),
        "-ac", str(AUDIO_CHANNELS),

        "-f", "flv",
        target,
    ]


def start_main_stream():
    log("[LIVE] Connecting to Restream.")
    log("[VIDEO] loop.mp4 repeats continuously.")
    log("[STORAGE] Audio tracks are not saved to disk.")

    return subprocess.Popen(
        build_ffmpeg_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=None,
        bufsize=0,
    )


def stop_process(proc):
    if proc and proc.poll() is None:
        proc.terminate()

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def stream_track(track, index, total):
    global main_ffmpeg

    log(f"[AUDIO] Track {index}/{total}: {track['title']}")

    downloader = None
    converter = None

    try:
        downloader_command = yt_dlp_base_args() + [
            "--no-playlist",
            "--retries", "3",
            "--fragment-retries", "3",
            "--socket-timeout", "20",
            "-f", "bestaudio/best",
            "-o", "-",
            track["url"],
        ]

        downloader = subprocess.Popen(
            downloader_command,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )

        converter_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-i", "pipe:0",
            "-vn",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(AUDIO_RATE),
            "-ac", str(AUDIO_CHANNELS),
            "pipe:1",
        ]

        converter = subprocess.Popen(
            converter_command,
            stdin=downloader.stdout,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )

        downloader.stdout.close()
        bytes_sent = 0

        while not stop_requested:
            if main_ffmpeg is None or main_ffmpeg.poll() is not None:
                raise RuntimeError("Main FFmpeg process stopped.")

            chunk = converter.stdout.read(65536)

            if not chunk:
                break

            main_ffmpeg.stdin.write(chunk)
            main_ffmpeg.stdin.flush()
            bytes_sent += len(chunk)

        converter_rc = converter.wait()
        downloader_rc = downloader.wait()

        if stop_requested:
            return True

        if bytes_sent == 0 or converter_rc != 0 or downloader_rc != 0:
            log(
                "[WARN] Audio extraction failed: "
                f"yt-dlp={downloader_rc}, "
                f"ffmpeg={converter_rc}, bytes={bytes_sent}"
            )
            return False

        log(f"[AUDIO] Track {index}/{total} finished.")
        return True

    except (BrokenPipeError, OSError, RuntimeError) as exc:
        log(f"[WARN] Track failed: {exc}")
        return False

    finally:
        stop_process(converter)
        stop_process(downloader)

        for proc in (converter, downloader):
            if proc and proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass


def stop_handler(signum, frame):
    global stop_requested
    stop_requested = True
    log("[SYSTEM] Stop signal received.")


def cleanup():
    global main_ffmpeg, cookies_path

    if main_ffmpeg:
        try:
            if main_ffmpeg.stdin:
                main_ffmpeg.stdin.close()
        except Exception:
            pass

        stop_process(main_ffmpeg)
        main_ffmpeg = None

    if cookies_path:
        try:
            os.remove(cookies_path)
        except OSError:
            pass
        cookies_path = None


def main():
    global main_ffmpeg

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    check_setup()
    make_cookie_file()

    while not stop_requested:
        try:
            tracks = get_playlist_tracks()
            main_ffmpeg = start_main_stream()
            index = 0

            while not stop_requested:
                if main_ffmpeg.poll() is not None:
                    raise RuntimeError(
                        "Live FFmpeg exited with code "
                        f"{main_ffmpeg.returncode}."
                    )

                success = stream_track(
                    tracks[index],
                    index + 1,
                    len(tracks),
                )

                if stop_requested:
                    break

                if not success:
                    log("[WARN] Skipping failed track.")
                    time.sleep(1)

                index = (index + 1) % len(tracks)

        except Exception as exc:
            log(f"[ERROR] {exc}")

            if not stop_requested:
                cleanup()
                log(f"[RETRY] Restarting in {RETRY_DELAY} seconds.")

                for _ in range(RETRY_DELAY):
                    if stop_requested:
                        break
                    time.sleep(1)

        finally:
            cleanup()

    log("[SYSTEM] Stopped.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"[FATAL] {exc}")
        cleanup()
        sys.exit(1)
