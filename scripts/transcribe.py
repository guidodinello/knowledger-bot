"""Transcribe a YouTube video using yt-dlp + faster-whisper.

Usage:
    uv run python scripts/transcribe.py <url> [-o out.txt]

Dependencies (install in project venv or use thesis venv):
    pip install faster-whisper yt-dlp
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from faster_whisper import WhisperModel


def _run_ytdlp(args: list[str], output_dir: Path) -> Path:
    result = subprocess.run(
        args,
        capture_output=True, text=True, check=True,
    )
    lines = [l for l in result.stdout.strip().split("\n") if l]
    return Path(lines[-1]) if lines else output_dir / "audio.wav"


def _default_args(url: str, output_dir: Path) -> list[str]:
    return [
        "yt-dlp",
        "-x", "--audio-format", "wav",
        "--audio-quality", "0",
        "-o", str(output_dir / "%(id)s.%(ext)s"),
        "--print", "filename",
        "--no-simulate",
        url,
    ]


def _android_fallback_args(url: str, output_dir: Path) -> list[str]:
    return [
        "yt-dlp",
        "--extractor-args", "youtube:player_client=android",
        "-f", "18",
        "-x", "--audio-format", "wav",
        "--audio-quality", "0",
        "-o", str(output_dir / "%(id)s.%(ext)s"),
        "--print", "filename",
        "--no-simulate",
        url,
    ]


def download_audio(url: str, output_dir: Path) -> Path:
    logging.info("Downloading audio from %s ...", url)

    strategies = [
        ("default", _default_args),
        ("android fallback", _android_fallback_args),
    ]

    for label, build_args in strategies:
        try:
            path = _run_ytdlp(build_args(url, output_dir), output_dir)
            logging.info("Downloaded with %s strategy: %s", label, path)
            return path
        except subprocess.CalledProcessError as e:
            logging.warning("%s strategy failed (exit %d)", label, e.returncode)

    raise RuntimeError(
        f"All download strategies failed for {url}. "
        "Try a newer yt-dlp version or use --cookies."
    )


def transcribe(audio_path: Path) -> str:
    logging.info("Loading WhisperModel medium (int8) ...")
    model = WhisperModel("medium", compute_type="int8", num_workers=4, cpu_threads=4)

    logging.info("Transcribing (language=es, no timestamps) ...")
    segments, _info = model.transcribe(
        str(audio_path),
        language="es",
        initial_prompt=None,
        word_timestamps=False,
        vad_filter=True,
    )

    lines = []
    for segment in segments:
        lines.append(segment.text.strip())

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe a YouTube video with faster-whisper")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    parser.add_argument("--model", default="medium", help="Whisper model size (default: medium)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    with tempfile.TemporaryDirectory(prefix="whisper_") as tmp:
        audio_path = download_audio(args.url, Path(tmp))
        text = transcribe(audio_path)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(text, encoding="utf-8")
        print(f"Saved to {out_path}")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
