"""One-shot CLI: fetch a YouTube transcript and upload it to a Claude project."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from knowledger.claude_client import AuthError, ClaudeClient
from knowledger.transcript import fetch_transcript
from knowledger.youtube import build_doc_name, extract_video_id, fetch_video_metadata


def _pick_project(client: ClaudeClient, name: str | None) -> tuple[str, str]:
    try:
        projects = client.projects
    except AuthError:
        raise
    except Exception as e:
        print(f"Error fetching projects: {e}", file=sys.stderr)
        sys.exit(1)
    if not projects:
        print("No Claude projects found.", file=sys.stderr)
        sys.exit(1)

    if name:
        match = next((p for p in projects if p["name"].lower() == name.lower()), None)
        if match is None:
            print(f"Project '{name}' not found. Available projects:", file=sys.stderr)
            for p in projects:
                print(f"  - {p['name']}", file=sys.stderr)
            sys.exit(1)
        return match["uuid"], match["name"]

    print("\nAvailable projects:")
    for i, p in enumerate(projects, 1):
        print(f"  {i}. {p['name']}")

    while True:
        try:
            raw = input("\nSelect project number: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(projects):
                return projects[idx]["uuid"], projects[idx]["name"]
        except ValueError:
            pass
        print("Invalid selection, try again.")


def main() -> None:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Upload a YouTube transcript to a Claude project")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--project", metavar="NAME", help="Claude project name (prompts if omitted)"
    )
    parser.add_argument("--cookies", metavar="PATH", help="Path to YouTube cookies file")
    args = parser.parse_args()

    session_token = os.getenv("CLAUDE_SESSION_TOKEN")
    if not session_token:
        print("Error: CLAUDE_SESSION_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    video_id = extract_video_id(args.url)
    if not video_id:
        print(f"Error: could not parse a video ID from '{args.url}'.", file=sys.stderr)
        sys.exit(1)

    cookies_path = (
        Path(args.cookies)
        if args.cookies
        else (Path(p) if (p := os.getenv("YOUTUBE_COOKIES_PATH")) else None)
    )
    if cookies_path is not None and not cookies_path.exists():
        print(f"Error: cookies file not found: {cookies_path}", file=sys.stderr)
        sys.exit(1)

    client = ClaudeClient(session_token)

    print("Fetching video metadata...")
    try:
        metadata = fetch_video_metadata(args.url)
    except Exception as e:
        print(f"Error fetching video info: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Title:   {metadata.title}")
    print(f"Channel: {metadata.channel_name}")

    try:
        project_id, project_name = _pick_project(client, args.project)
    except AuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nFetching transcript...")
    try:
        transcript = fetch_transcript(video_id, cookies_path=cookies_path)
    except Exception as e:
        print(f"Error fetching transcript: {e}", file=sys.stderr)
        sys.exit(1)
    if transcript is None:
        print("Error: no captions available for this video.", file=sys.stderr)
        sys.exit(1)

    file_name = build_doc_name(metadata.channel_name, metadata.title, metadata.upload_date)

    print(f"Uploading to '{project_name}'...")
    try:
        client.upload_content(project_id, transcript, file_name)
    except AuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Upload failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Done. Saved as '{file_name}'.")


if __name__ == "__main__":
    main()
