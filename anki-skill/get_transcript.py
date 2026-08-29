#!/usr/bin/env python3
"""
Fetch a YouTube transcript with clickable timestamps.

Usage:
    python get_transcript.py <url-or-video-id> [--format text|json|plain]

Requires:
    pip install youtube-transcript-api

Output (default 'text' format):
    [1:23](https://youtu.be/VIDEO_ID?t=83) transcript text here ...

The timestamp links are Markdown hyperlinks — they open the video at
the exact second in any Markdown renderer or Claude's chat window.
"""

import sys
import json
import re
import argparse


def extract_video_id(url_or_id: str) -> str:
    patterns = [
        r"(?:v=|/shorts/|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        m = re.search(pattern, url_or_id)
        if m:
            return m.group(1)
    raise ValueError(f"Could not extract video ID from: {url_or_id}")


def seconds_to_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fetch_transcript(video_id: str):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        print(
            "ERROR: youtube-transcript-api is not installed.\n"
            "Run: pip install youtube-transcript-api",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return YouTubeTranscriptApi.get_transcript(video_id)
    except Exception as e:
        # Try without language preference
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = transcript_list.find_generated_transcript(
                ["en", "en-US", "en-GB"]
            )
            return transcript.fetch()
        except Exception:
            print(f"ERROR: Could not fetch transcript — {e}", file=sys.stderr)
            sys.exit(1)


def format_text(transcript, video_id: str) -> str:
    lines = []
    for entry in transcript:
        ts = seconds_to_timestamp(entry["start"])
        t_int = int(entry["start"])
        link = f"https://youtu.be/{video_id}?t={t_int}"
        text = entry["text"].replace("\n", " ").strip()
        lines.append(f"[{ts}]({link}) {text}")
    return "\n".join(lines)


def format_plain(transcript) -> str:
    """Plain text with no links — useful for feeding into other tools."""
    return " ".join(
        entry["text"].replace("\n", " ").strip() for entry in transcript
    )


def main():
    parser = argparse.ArgumentParser(
        description="Fetch a YouTube transcript with timestamps."
    )
    parser.add_argument("url", help="YouTube URL or 11-char video ID")
    parser.add_argument(
        "--format",
        choices=["text", "json", "plain"],
        default="text",
        help="Output format (default: text with Markdown timestamp links)",
    )
    args = parser.parse_args()

    video_id = extract_video_id(args.url)
    transcript = fetch_transcript(video_id)

    if args.format == "json":
        print(json.dumps(transcript, indent=2, ensure_ascii=False))
    elif args.format == "plain":
        print(format_plain(transcript))
    else:
        print(format_text(transcript, video_id))


if __name__ == "__main__":
    main()
