"""Minimal device/source client for Dounai Input Gateway.

Usage:
    export DOUNAI_API_TOKEN="..."
    python examples/device_client.py text "今天有点累"
    python examples/device_client.py image ./photo.jpg
    python examples/device_client.py audio ./voice.wav

Requires the gateway's runtime dependency ``httpx``; no extra package is needed.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

import httpx

DEFAULT_BASE_URL = os.getenv("DOUNAI_BASE_URL", "http://127.0.0.1:8000")
SOURCE = "device:example-client"


def auth_headers() -> dict[str, str]:
    token = os.getenv("DOUNAI_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a text/image/audio input.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--session", default="sess_device_demo")
    sub = parser.add_subparsers(dest="kind", required=True)
    text = sub.add_parser("text")
    text.add_argument("text")
    image = sub.add_parser("image")
    image.add_argument("path")
    audio = sub.add_parser("audio")
    audio.add_argument("path")
    audio.add_argument("--language", default="zh")
    args = parser.parse_args()

    headers = auth_headers()
    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        if args.kind == "text":
            response = client.post(
                "/v1/inputs/text",
                headers=headers,
                json={"text": args.text, "session_id": args.session, "source": SOURCE},
            )
        else:
            path = Path(args.path)
            if not path.is_file():
                print(f"file not found: {path}", file=sys.stderr)
                return 2
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = {"session_id": args.session, "source": SOURCE}
            if args.kind == "audio":
                data["language"] = args.language
            with path.open("rb") as handle:
                response = client.post(
                    f"/v1/inputs/{args.kind}",
                    headers=headers,
                    data=data,
                    files={"file": (path.name, handle, mime)},
                )
    print(response.status_code)
    print(response.text)
    print(
        "degraded=",
        response.headers.get("X-Dounai-Degraded"),
        "provider=",
        response.headers.get("X-Dounai-Provider"),
        "session=",
        response.headers.get("X-Dounai-Session"),
    )
    return 0 if response.status_code == 201 else 1


if __name__ == "__main__":
    raise SystemExit(main())
