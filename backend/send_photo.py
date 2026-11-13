#!/usr/bin/env python3
"""Send a photo to the local coloring worker and save the returned PNG.

Usage:
    python send_photo.py photo.jpg
    python send_photo.py photo.jpg -o page.png
"""
import argparse
import sys
import time
from pathlib import Path

import requests

DEFAULT_URL = "http://127.0.0.1:5001/generate"


def main():
    parser = argparse.ArgumentParser(description="Convert a photo to a coloring page.")
    parser.add_argument("photo", type=Path, help="path to the input photo")
    parser.add_argument("-o", "--output", type=Path, help="output PNG path (default: <photo>_coloring.png)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"worker endpoint (default: {DEFAULT_URL})")
    args = parser.parse_args()

    if not args.photo.is_file():
        sys.exit(f"Photo not found: {args.photo}")

    output = args.output or args.photo.with_name(f"{args.photo.stem}_coloring.png")

    start = time.perf_counter()
    try:
        with args.photo.open("rb") as f:
            resp = requests.post(args.url, files={"image": (args.photo.name, f)}, timeout=300)
    except requests.exceptions.ConnectionError:
        sys.exit(f"Could not connect to {args.url}. Is the worker running?")

    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        sys.exit(f"Worker returned {resp.status_code}: {detail}")

    output.write_bytes(resp.content)
    print(f"Saved {output} ({time.perf_counter() - start:.1f}s round trip)")


if __name__ == "__main__":
    main()
