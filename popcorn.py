#!/usr/bin/env python3
"""
Popcorn — search torrent sites for a movie and download via magnet link.

Usage:
    python popcorn.py "movie name"
    python popcorn.py "Inception 2010" --source tpb

Sources: tpb (The Pirate Bay JSON API, default), BitSearch (JSON API),
rarbg (scraped), yts (API), tgx (scraped), and torrents-csv (JSON API). The
Pirate Bay, BitSearch and Torrents-CSV APIs embed infohashes directly, so
magnet links are built without a second request.
Download: passes the magnet link to aria2c (must be installed).
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import requests

# ── data ────────────────────────────────────────────────────────────────────


from sources import Release as TorrentResult, SearchContext, service as source_service
from downloader import build_magnet as _build_magnet, magnet_for_release

__all__ = ["TorrentResult", "SearchContext", "source_service", "_build_magnet", "magnet_for_result"]


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}

# Set by main() when --proxy / --timeout are passed.
_PROXY: dict | None = None
_TIMEOUT = 15

# DNS-over-HTTPS support. When enabled, socket.getaddrinfo is replaced
# so all hostname lookups go through Cloudflare DoH, bypassing local DNS
# blocks.  The monkey-patch is applied in main() when --doh is passed.
_original_getaddrinfo = socket.getaddrinfo
_DOH = False  # set True when --doh flag is active
_DOH_CACHE: dict[str, tuple[float, list[str]]] = {}
_DOH_CACHE_LOCK = threading.Lock()


def _resolve_doh(hostname: str) -> list[str]:
    """Resolve a hostname to IPv4 addresses via Cloudflare DoH JSON API."""
    with _DOH_CACHE_LOCK:
        cached = _DOH_CACHE.get(hostname)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    url = f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A"
    try:
        resp = requests.get(
            url,
            headers={"Accept": "application/dns-json"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        addresses = [
            ans["data"]
            for ans in data.get("Answer", [])
            if ans.get("type") == 1
        ]
        with _DOH_CACHE_LOCK:
            _DOH_CACHE[hostname] = (time.monotonic() + 300, addresses)
        return addresses
    except Exception:
        return []


def _doh_getaddrinfo(host: str, port, family=0, type=0, proto=0, flags=0):
    """socket.getaddrinfo replacement that uses DoH for non-local hosts."""
    if not host or host in (
        "127.0.0.1", "::1", "localhost", "0.0.0.0", "cloudflare-dns.com"
    ):
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    ips = _resolve_doh(str(host))
    if ips:
        results = []
        for ip in ips:
            results.append(
                (socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port))
            )
        return results

    # DoH failed — fall back to system resolver
    return _original_getaddrinfo(host, port, family, type, proto, flags)


def _get(url: str, **kwargs) -> requests.Response:
    """Thin wrapper around requests.get that applies proxy/timeout config."""
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", _TIMEOUT)
    if _PROXY:
        kwargs["proxies"] = _PROXY
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.InvalidSchema as e:
        if "socks" in str(e).lower():
            print("  SOCKS proxy requires PySocks — run: pip install 'requests[socks]'")
        raise


# ── connectivity check ─────────────────────────────────────────────────────


def _check_internet() -> bool:
    """Quick check that we can make HTTPS requests at all."""
    for url in ("https://httpbin.org/ip", "https://api.ipify.org"):
        try:
            _get(url, timeout=8)
            return True
        except requests.RequestException:
            continue
    return False


# Compatibility entrypoints for existing CLI/import callers.
def _search(source, query):
    return source_service.search(SearchContext(query), [source],
                                 deadline_seconds=_TIMEOUT).releases


def search_apibay(query):
    return _search("tpb", query)


def search_bitsearch(query):
    return _search("regional", query)


def search_rarbg(query):
    return _search("rarbg", query)


def search_yts(query):
    return _search("yts", query)


def search_tg(query):
    return _search("tgx", query)


def search_torrents_csv(query):
    return _search("torrents-csv", query)


# ── display ─────────────────────────────────────────────────────────────────


def _pad(s: str, width: int) -> str:
    """Truncate and pad a string to a fixed display width."""
    if len(s) > width:
        return s[: width - 1] + "…"
    return s.ljust(width)


def show_results(results: list[TorrentResult]):
    """Print a formatted table of results."""
    if not results:
        print("  No results found.")
        return

    # fixed column widths
    w_title = 55
    w_seeds = 7
    w_peers = 7
    w_size = 10
    w_src = 12  # fits "Torrents-CSV", the longest source label

    header = (
        f"  {'#':>3}  {_pad('Title', w_title)} {_pad('Seeds', w_seeds)} "
        f"{_pad('Peers', w_peers)} {_pad('Size', w_size)} {_pad('Source', w_src)}"
    )
    sep = "-" * len(header)

    print(sep)
    print(header)
    print(sep)

    for i, r in enumerate(results, 1):
        line = (
            f"  {i:>3}  {_pad(r.title, w_title)} "
            f"{_pad(str(r.seeds), w_seeds)} "
            f"{_pad(str(r.peers), w_peers)} "
            f"{_pad(r.size, w_size)} "
            f"{_pad(r.source, w_src)}"
        )
        print(line)

    print(sep)


# ── download ────────────────────────────────────────────────────────────────


def magnet_for_result(r: TorrentResult) -> str | None:
    """Resolve only when the index omitted a hash, then build a magnet locally."""
    if r.source in {"RARBG", "TGx"}:
        r.source = {"RARBG": "rarbg", "TGx": "tgx"}[r.source]
    return magnet_for_release(source_service.resolve(r))


ARIA2_FLAGS = [
    "--seed-time=0",
    # ── speed tuning ──
    "--max-overall-download-limit=0",     # no cap
    "--max-download-limit=0",             # no per-download cap either
    f"--bt-max-peers={os.environ.get('POPCORN_ARIA2_MAX_PEERS', '80')}",
    "--disk-cache=64M",                   # smooth bursty peer and disk throughput
    "--continue=true",                    # reuse verified pieces after an interruption
    "--optimize-concurrent-downloads=true",
    "--enable-dht=true",
    "--enable-peer-exchange=true",
    f"--bt-enable-lpd={os.environ.get('POPCORN_ARIA2_LPD', 'false')}",
    f"--file-allocation={os.environ.get('POPCORN_ARIA2_ALLOCATION', 'falloc')}",
    "--auto-save-interval=10",
    "--bt-save-metadata=true",
    "--check-integrity=true",
    "--allow-overwrite=false",
    "--auto-file-renaming=false",
    "--connect-timeout=10",
    "--timeout=60",
    "--max-tries=5",
    "--retry-wait=10",
    # Fixed ports so you can open them in the firewall (see HANDOFF notes).
    "--listen-port=6881-6889",
    "--dht-listen-port=6881-6889",
]


def _rclone_move(local_dir: str, remote: str) -> bool:
    """Move everything in local_dir to an rclone remote, then return success."""
    print(f"\n  Uploading to {remote} via rclone...\n")
    try:
        rc = subprocess.run(
            ["rclone", "move", local_dir, remote, "-P",
             "--transfers=8", "--drive-chunk-size=128M"],
            check=False,
        ).returncode
    except FileNotFoundError:
        print(
            "  rclone not found. Install it:\n"
            "    curl https://rclone.org/install.sh | sudo bash\n"
            f"  Your download is still on local disk: {local_dir}\n"
        )
        return False
    if rc != 0:
        print(f"  rclone failed (exit {rc}). Files kept locally: {local_dir}")
        return False
    return True


def download_magnet(magnet: str, upload_to: str | None = None):
    """Launch aria2c with the magnet link.

    If upload_to is given (e.g. 'gdrive:Movies'), download into a temp dir,
    then rclone-move the result to that remote and delete the local copy.
    """
    dl_dir = tempfile.mkdtemp(prefix="grabber-", dir=".") if upload_to else None
    cmd = ["aria2c", *ARIA2_FLAGS]
    if dl_dir:
        cmd.append(f"--dir={dl_dir}")
    cmd.append(magnet)

    print("\n  Launching aria2c...\n")
    try:
        rc = subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        print(
            "  aria2c not found. Install it:\n"
            "    sudo apt install aria2    # Debian/Ubuntu\n"
            "    brew install aria2        # macOS\n"
            "  Then paste this magnet link into your torrent client:\n"
            f"\n  {magnet}\n"
        )
        sys.exit(1)

    if dl_dir:
        if rc == 0 and _rclone_move(dl_dir, upload_to):
            shutil.rmtree(dl_dir, ignore_errors=True)
            print(f"  Done — uploaded to {upload_to} and cleared local copy.")
        else:
            print(f"  Download incomplete or upload skipped; kept {dl_dir}")


# ── main ────────────────────────────────────────────────────────────────────


def main():
    global _PROXY, _TIMEOUT

    parser = argparse.ArgumentParser(
        description="Search and download movies from torrent sites.",
        epilog="Tip: if sources are blocked, use --proxy to route through a VPN/SOCKS proxy.",
    )
    parser.add_argument(
        "query", nargs="+", help="Movie name / search query"
    )
    parser.add_argument(
        "--source",
        choices=[*source_service.providers, "all"],
        default="tpb",
        help="Torrent source to search (default: tpb — The Pirate Bay API). "
        "Use regional for Malayalam/Tamil/Telugu films, rarbg for mainstream "
        "movie and TV releases",
    )
    parser.add_argument(
        "--proxy",
        metavar="URL",
        help="Proxy URL (e.g. socks5h://127.0.0.1:9050 or http://proxy:8080)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        metavar="SEC",
        help="Request timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--doh",
        action="store_true",
        help="Resolve hostnames via DNS-over-HTTPS (bypasses DNS blocks)",
    )
    parser.add_argument(
        "--upload-to",
        metavar="REMOTE",
        help="rclone remote to move the finished download to, then delete "
        "local copy (e.g. gdrive:Movies). Requires rclone configured.",
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="Search and display results, then exit (no download).",
    )
    parser.add_argument(
        "--pick",
        type=int,
        metavar="N",
        help="Skip interactive picker — download result N (1-indexed).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON (only meaningful with --search-only).",
    )
    args = parser.parse_args()

    query = " ".join(args.query)

    if args.proxy:
        _PROXY = {"http": args.proxy, "https": args.proxy}
        os.environ["POPCORN_SOURCE_PROXY"] = args.proxy
    _TIMEOUT = args.timeout

    if args.doh:
        from sources import transport
        transport.LEGACY_CLI_DOH = True
        socket.getaddrinfo = _doh_getaddrinfo
        print("Using DNS-over-HTTPS (Cloudflare) for hostname resolution.\n")

    names = list(source_service.providers) if args.source == "all" else [args.source]
    all_results = source_service.search(
        SearchContext(query), names, deadline_seconds=args.timeout
    ).releases

    if not all_results:
        print("No matching releases available. See source logs for provider diagnostics.")
        sys.exit(0)

    show_results(all_results)

    if args.search_only:
        if args.json:
            import json
            out = []
            for r in all_results:
                out.append({
                    "title": r.title,
                    "seeds": r.seeds,
                    "peers": r.peers,
                    "size": r.size,
                    "source": r.source,
                    "magnet": r.magnet[:80] + "..." if r.magnet else None,
                })
            print("\n__JSON_OUTPUT__")
            print(json.dumps(out, indent=2))
        sys.exit(0)

    # pick one
    if args.pick is not None:
        idx = args.pick - 1
        if idx < 0 or idx >= len(all_results):
            print(f"  Invalid pick: {args.pick}. Must be 1-{len(all_results)}.")
            sys.exit(1)
    else:
        while True:
            try:
                choice = input(
                    f"\n  Enter number to download (1-{len(all_results)}, 'q' to quit): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)

            if choice.lower() == "q":
                print("  Bye.")
                sys.exit(0)

            try:
                idx = int(choice) - 1
                if 0 <= idx < len(all_results):
                    break
            except ValueError:
                pass

            print(f"  Invalid. Enter a number between 1 and {len(all_results)}.")

    selected = all_results[idx]
    print(f'\n  Selected: {selected.title}')
    print(f"  Seeds: {selected.seeds}  Peers: {selected.peers}  Size: {selected.size}")

    magnet = magnet_for_result(selected)
    if not magnet:
        print("  Could not obtain magnet link.")
        sys.exit(1)

    print(f"  Magnet: {magnet[:80]}...")
    download_magnet(magnet, upload_to=args.upload_to)


if __name__ == "__main__":
    main()
