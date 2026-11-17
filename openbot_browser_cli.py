#!/usr/bin/env python3
"""The OpenBot browser, for a Herdr agent during an OpenBot request (herdr_browser.py).

    python3 openbot_browser_cli.py <turn file> navigate <url>
    python3 openbot_browser_cli.py <turn file> read
    python3 openbot_browser_cli.py <turn file> screenshot

The turn file is written by the gateway for one request; it holds only the gateway's local address.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("navigate", "read", "screenshot") or (argv[1] == "navigate") != (len(argv) == 3):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as handle:
            endpoint = json.load(handle)["endpoint"]
    except (OSError, ValueError, KeyError, TypeError):
        print("This browser session has ended. Browsing works only during an OpenBot request.", file=sys.stderr)
        return 1
    body = {"action": argv[1], **({"url": argv[2]} if argv[1] == "navigate" else {})}
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                                     headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read() or b"{}").get("error")
        except ValueError:
            message = None
        print(message or f"The OpenBot browser refused the request ({error.code}).", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError):
        print("Could not reach the Agent Fabric gateway.", file=sys.stderr)
        return 1
    if argv[1] == "screenshot":
        print(f"Saved {result.get('saved')} of {result.get('url')}. It is attached to your reply automatically.")
    else:
        print(f"Title: {result.get('title', '')}\nURL: {result.get('url', '')}\n\n{result.get('text', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
