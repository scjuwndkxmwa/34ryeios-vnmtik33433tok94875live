
import os
import sys
import subprocess
from pathlib import Path

# ============== SETTINGS ==============

PLAYLIST_URL = (
    "https://youtube.com/playlist?"
    "list=PLbB-zzxmyt-qxqhYuwRWiDCAFgH8bDgkR"
)

RESTREAM_KEY = "re_12012590_event263b321ab25446e5b8f3f975dabd2c6a"
RESTREAM_SERVER = "rtmp://live.restream.io/live"

PROJECT_DIR = Path(__file__).resolve().parent
VIDEO_FILE = PROJECT_DIR / "loop.mp4"

DATA_DIR = Path("/data")
AUDIO_DIR = DATA_DIR / "playlist_audio"
COMBINED_AUDIO = DATA_DIR / "playlist_complete.mp3"
CONCAT_FILE = DATA_DIR / "concat_list.txt"
DONE_FILE = DATA_DIR / "playlist_finished.txt"

# ======================================


def run(command):
    print("[RUN]", " ".join(map(str, command)), flush=True)
    subprocess.run(command, check=True)


def check_setup():
    if not VIDEO_FILE.is_file():
        raise FileNotFoundError(
            "Missing loop.mp4. Upload it beside main.py in GitHub."
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    for tool in ("ffmpeg", "yt-dlp"):
        result = subprocess.run(
            ["sh", "-c", f"command -v {tool}"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Required tool missing: {tool}")


def download_playlist():
    if COMBINED_AUDIO.is_file() and COMBINED_AUDIO.stat().st_size > 0:
        print("[AUDIO] Reusing saved playlist.", flush=True)
        return

    print("[DOWNLOAD] Downloading playlist audio.", flush=True)

    run([
        "yt-dlp",
        "--yes-playlist",
        "--ignore-errors",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "128K",
        "--output",
        str(AUDIO_DIR / "%(playlist_index)05d-%(id)s.%(ext)s"),
        PLAYLIST_URL,
    ])

    audio_files = sorted(AUDIO_DIR.glob("*.mp3"))

    if not audio_files:
        raise RuntimeError("No audio files downloaded.")

    print(f"[DOWNLOAD] Found {len(audio_files)} tracks.", flush=True)

    with CONCAT_FILE.open("w", encoding="utf-8") as f:
        for audio in audio_files:
            f.write(f"file '{audio.as_posix()}'\n")

    temp_audio = DATA_DIR / "playlist_complete.tmp.mp3"

    run([
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(CONCAT_FILE),
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "160k",
        str(temp_audio),
    ])

    temp_audio.replace(COMBINED_AUDIO)
    print("[AUDIO] Ordered playlist assembled.", flush=True)


def start_stream():
    if DONE_FILE.exists():
        print("[DONE] Playlist already played successfully.", flush=True)
        return

    if not COMBINED_AUDIO.is_file():
        raise FileNotFoundError("Combined playlist audio is missing.")

    output_url = f"{RESTREAM_SERVER}/{RESTREAM_KEY}"

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-nostdin",

        # Repeat video forever
        "-stream_loop", "-1",
        "-re",
        "-i", str(VIDEO_FILE),

        # Play the complete audio playlist once
        "-re",
        "-i", str(COMBINED_AUDIO),

        "-map", "0:v:0",
        "-map", "1:a:0",

        # TikTok vertical format
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
        "-b:a", "160k",
        "-ar", "48000",
        "-ac", "2",

        # Stop output when audio ends
        "-shortest",
        "-f", "flv",
        output_url,
    ]

    print("[LIVE] Loop video + ordered playlist audio.", flush=True)
    run(command)

    DONE_FILE.write_text("finished\n", encoding="utf-8")
    print("[DONE] Playlist finished.", flush=True)


def main():
    check_setup()

    if DONE_FILE.exists():
        print("[DONE] Already completed; not replaying.", flush=True)
        return

    download_playlist()
    start_stream()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Process failed: {exc}", flush=True)
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] {exc}", flush=True)
        sys.exit(1)
