"""Transcribe a YouTube video or Instagram Reel using yt-dlp + faster-whisper.

Usage:
    scripts/transcribe.sh <url> [-o out.txt] [--model medium] [--device cuda|cpu]
                          [--cookies cookies.txt] [--language es]

Instagram Reels have no caption API and sit behind a login-wall even when publicly
viewable in a browser — pass --cookies with a Netscape-format cookie file (e.g. exported
via a "Get cookies.txt" browser extension while logged into an account that can view the
Reel). --language defaults to Whisper auto-detection; pass a code (es, en, ...) to force it.

Setup (one-time):
    python3 -m venv .venv-transcribe
    .venv-transcribe/bin/pip install faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 yt-dlp

The transcribe.sh wrapper sets LD_LIBRARY_PATH for the CUDA 12 libs bundled in .venv-transcribe.
"""

import argparse
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import ctranslate2
from faster_whisper import WhisperModel


def _run_ytdlp(args: list[str], output_dir: Path) -> Path:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in result.stdout.strip().split("\n") if line]
    return Path(lines[-1]) if lines else output_dir / "audio.wav"


def _default_args(url: str, output_dir: Path) -> list[str]:
    return [
        "yt-dlp",
        "-x",
        "--audio-format",
        "wav",
        "--audio-quality",
        "0",
        "-o",
        str(output_dir / "%(id)s.%(ext)s"),
        "--print",
        "after_move:filepath",
        "--no-simulate",
        url,
    ]


def _android_fallback_args(url: str, output_dir: Path) -> list[str]:
    return [
        "yt-dlp",
        "--extractor-args",
        "youtube:player_client=android",
        "-f",
        "18",
        "-x",
        "--audio-format",
        "wav",
        "--audio-quality",
        "0",
        "-o",
        str(output_dir / "%(id)s.%(ext)s"),
        "--print",
        "after_move:filepath",
        "--no-simulate",
        url,
    ]


def _cookies_args(url: str, output_dir: Path, cookies_path: Path) -> list[str]:
    return [
        "yt-dlp",
        "--cookies",
        str(cookies_path),
        "-x",
        "--audio-format",
        "wav",
        "--audio-quality",
        "0",
        "-o",
        str(output_dir / "%(id)s.%(ext)s"),
        "--print",
        "after_move:filepath",
        "--no-simulate",
        url,
    ]


def download_audio(url: str, output_dir: Path, cookies_path: Path | None = None) -> Path:
    logging.info("Downloading audio from %s ...", url)

    if cookies_path is not None:
        # Cookies auth replaces (not adds to) the android fallback, which is YouTube-only
        # and would just waste a request against Instagram's login-wall.
        strategies = [("cookies", lambda u, d: _cookies_args(u, d, cookies_path))]
    else:
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
        f"All download strategies failed for {url}. Try a newer yt-dlp version or use --cookies."
    )


def _pick_device() -> tuple[str, str, dict]:
    gpu_count = ctranslate2.get_cuda_device_count()
    if gpu_count > 0:
        logging.info("GPU detected (%d device(s)) — using float16", gpu_count)
        return "cuda", "float16", {}
    logging.info("No GPU detected — using CPU int8")
    return "cpu", "int8", {"num_workers": 4, "cpu_threads": 4}


def transcribe(
    audio_path: Path,
    model_size: str = "medium",
    device: str | None = None,
    language: str | None = None,
) -> str:
    if device:
        dev, ctype, extra = device, "float16", {}
    else:
        dev, ctype, extra = _pick_device()

    logging.info("Loading WhisperModel %s (%s, %s) ...", model_size, dev, ctype)
    model = WhisperModel(model_size, device=dev, compute_type=ctype, **extra)

    logging.info("Transcribing (language=%s, no timestamps) ...", language or "auto-detect")
    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        initial_prompt=None,
        word_timestamps=False,
        vad_filter=True,
    )

    lines = []
    for segment in segments:
        lines.append(segment.text.strip())

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe a YouTube video or Instagram Reel with faster-whisper"
    )
    parser.add_argument("url", help="YouTube or Instagram Reel URL")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    parser.add_argument("--model", default="medium", help="Whisper model size (default: medium)")
    parser.add_argument("--device", choices=["cuda", "cpu"], help="Override device auto-detection")
    parser.add_argument(
        "--cookies",
        help="Netscape-format cookies file (required for Instagram; unused for YouTube)",
    )
    parser.add_argument(
        "--language", help="Force transcription language (e.g. es, en); default: auto-detect"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cookies_path = Path(args.cookies) if args.cookies else None
    with tempfile.TemporaryDirectory(prefix="whisper_") as tmp:
        audio_path = download_audio(args.url, Path(tmp), cookies_path=cookies_path)
        text = transcribe(
            audio_path, model_size=args.model, device=args.device, language=args.language
        )

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(text, encoding="utf-8")
        print(f"Saved to {out_path}")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
