"""One bounded HTTP exchange. stdin carries credentials, never argv/logs.

The parent can kill this worker on its absolute deadline, including DNS stalls,
redirect chains and slowly trickling response bodies that read timeouts miss.
"""

import json
import sys
import time
import socket
from pathlib import Path

import requests


def main():
    args = json.load(sys.stdin)
    if args.get("legacy_cli_doh"):
        # Preserve the existing explicit CLI flag inside this isolated process.
        # Web requests never enable it or alter the service's resolver.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import popcorn

        socket.getaddrinfo = popcorn._doh_getaddrinfo
    deadline = time.monotonic() + args["total_timeout"]
    try:
        with requests.Session() as session:
            session.max_redirects = 3
            if args.get("proxy"):
                session.proxies.update({"http": args["proxy"], "https": args["proxy"]})
            with session.get(
                args["url"],
                params=args["params"],
                headers=args["headers"],
                timeout=(args["connect_timeout"], args["read_timeout"]),
                stream=True,
            ) as response:
                chunks, length = [], 0
                for chunk in response.iter_content(16384):
                    length += len(chunk)
                    if length > args["max_bytes"]:
                        raise ValueError("invalid_payload")
                    if time.monotonic() >= deadline:
                        raise requests.Timeout()
                    chunks.append(chunk)
                payload = {
                    "status": response.status_code,
                    "url": response.url,
                    "content_type": response.headers.get("Content-Type", ""),
                    "retry_after": response.headers.get("Retry-After", "0"),
                    "body": b"".join(chunks).decode(
                        response.encoding or "utf-8", errors="replace"
                    ),
                }
    except requests.Timeout:
        payload = {"error": "timeout", "retryable": True}
    except requests.TooManyRedirects:
        payload = {"error": "redirect_loop"}
    except requests.exceptions.SSLError:
        payload = {"error": "tls"}
    except requests.ConnectionError:
        payload = {"error": "network", "retryable": True}
    except requests.RequestException:
        payload = {"error": "request"}
    except Exception:
        payload = {"error": "invalid_payload"}
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
