"""Container/host health probe. Exit 0 only if /healthz answers 200 with the generic body."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000/healthz"


def check(url: str, timeout: float = 3.0) -> bool:
    if not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        return False  # only ever probe loopback
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read(1024))
            return bool(response.status == 200 and body == {"status": "ok"})
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    if check(args.url):
        print("healthy")
        return 0
    print("unhealthy")
    return 1


if __name__ == "__main__":
    sys.exit(main())
