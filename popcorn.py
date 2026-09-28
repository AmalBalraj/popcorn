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
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

import requests
from bs4 import BeautifulSoup

# ── data ────────────────────────────────────────────────────────────────────


@dataclass
class TorrentResult:
    title: str
    seeds: int
    peers: int
    size: str
    magnet: str | None = None
    source: str = ""
    url: str = ""


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


# theRARBG — the maintained successor to RARBG, carrying the same release
# groups and clean 1080p/2160p movie and TV encodes. Plain HTTP, no API key
# and no Cloudflare challenge, so it works from datacenter IPs where 1337x's
# Cloudflare "Just a moment..." challenge blocks every mirror. Replaces the
# old MIRRORS_1337X list (all of which now 403 or no longer resolve).
MIRRORS_RARBG = [
    "https://therarbg.com",
]

MIRRORS_YTS = [
    "https://movies-api.accel.li",
    "https://yts.lt",
    "https://yts.bz",
    "https://yts.rs",
    "https://yts.do",
]

# apibay.org is the only host that still serves the /q.php JSON API — the
# pirate-bay proxies that used to mirror it (thepiratebay10.info/apibay,
# tpb.party, thehiddenbay.com) now return 404 for that path.
MIRRORS_APIBAY = [
    "https://apibay.org",
]

# apibay.org is intermittently slow under load and can exceed the search
# timeout, so a single miss is retried rather than reported as no results.
#
# Each attempt gets its own short cap rather than the full search timeout.
# A healthy apibay answers in ~0.4s, so the cap costs nothing in the normal
# case, while an unresponsive one used to burn the whole timeout twice —
# measured at 20-30s for a search whose other five sources had all answered
# inside 2s. Sources run in parallel, so the slowest one sets the total.
_APIBAY_ATTEMPTS = 2
_APIBAY_TIMEOUT = 6

BITSEARCH_API = "https://bitsearch.eu/api/v1/search"
_BITSEARCH_CACHE: dict[str, tuple[float, list[TorrentResult]]] = {}
_BITSEARCH_CACHE_LOCK = threading.Lock()

# Torrents-CSV — a JSON index built from DHT scrapes. Replaces TamilBlasters,
# whose forum went down and whose domain now 522s everywhere (the .bet entry
# domain redirects to .garden, which is dead at the origin). Unlike the forum
# it reports real seeder counts, needs no --doh, and still covers the regional
# titles TamilBlasters was added for.
TORRENTS_CSV_API = "https://torrents-csv.com/service/search"

# Public trackers appended to magnet links so aria2c can find peers without
# relying solely on DHT (which is often firewalled).
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]


def _build_magnet(infohash: str, name: str) -> str:
    """Build a magnet URI from an infohash, with display name and trackers."""
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={quote_plus(name)}"
    for tr in TRACKERS:
        magnet += f"&tr={quote_plus(tr)}"
    return magnet


def _human_size(num_bytes) -> str:
    """Format a byte count as a human-readable size."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _int_or_zero(value) -> int:
    """Read an integer from a table cell or JSON value; anything else is 0."""
    if hasattr(value, "get_text"):
        value = value.get_text(strip=True)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── theRARBG search ─────────────────────────────────────────────────────────


def _scrape_rarbg(base: str, query: str, category: str) -> list[TorrentResult]:
    """Scrape one theRARBG category listing.

    The listing has no magnet links — each row links to a /post-detail/ page
    that carries one, fetched lazily on selection like the old 1337x source.
    """
    url = f"{base}/get-posts/keywords:{quote_plus(query)}:category:{category}/"
    resp = _get(url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[TorrentResult] = []

    for row in soup.select("tr.list-entry"):
        link = row.select_one("td.cellName a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title or not href:
            continue

        size_cell = row.select_one("td.sizeCell")
        # Seeder and leecher counts are the only green/red cells in the row.
        seeds = _int_or_zero(row.select_one("td[style*='color: green']"))
        peers = _int_or_zero(row.select_one("td[style*='color: red']"))

        results.append(
            TorrentResult(
                title=title,
                seeds=seeds,
                peers=peers,
                size=size_cell.get_text(strip=True) if size_cell else "?",
                source="RARBG",
                url=base + href if href.startswith("/") else href,
            )
        )
    return results


def search_rarbg(query: str) -> list[TorrentResult]:
    """Search theRARBG's movie and TV categories and merge the two listings."""
    for base in MIRRORS_RARBG:
        results: list[TorrentResult] = []
        seen: set[str] = set()
        for category in ("Movies", "TV"):
            try:
                found = _scrape_rarbg(base, query, category)
            except requests.RequestException as e:
                # A failure on one category shouldn't discard the other's rows.
                print(f"  RARBG: {base} {category} — {e}")
                continue
            for item in found:
                if item.url not in seen:
                    seen.add(item.url)
                    results.append(item)
        if results:
            print(f"  RARBG: connected via {base} ({len(results)} results)")
            return results
    print("  RARBG: all mirrors failed")
    return []


def fetch_rarbg_magnet(torrent_url: str) -> str | None:
    """Visit a theRARBG post page and extract the magnet link."""
    try:
        resp = _get(torrent_url)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Failed to fetch torrent page: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    magnet = soup.select_one("a[href^='magnet:']")
    if magnet:
        return magnet["href"]

    # fallback: search all links
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("magnet:"):
            return a["href"]

    return None


# ── YTS search ──────────────────────────────────────────────────────────────


def _query_yts_mirror(base: str, query: str) -> list[TorrentResult]:
    """Query a single YTS mirror API."""
    url = f"{base}/api/v2/list_movies.json"
    results: list[TorrentResult] = []

    try:
        resp = _get(
            url, params={"query_term": query, "limit": 20}
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  YTS: {base} — {e}")
        return results

    if data.get("status") != "ok":
        return results

    for movie in data.get("data", {}).get("movies", []):
        for torrent in movie.get("torrents", []):
            title = f"{movie['title']} ({movie['year']}) — {torrent['quality']} {torrent['type']}"
            results.append(
                TorrentResult(
                    title=title,
                    seeds=torrent.get("seeds", 0),
                    peers=torrent.get("peers", 0),
                    size=torrent.get("size", "?"),
                    magnet=torrent.get("hash", ""),  # will convert later
                    source="yts",
                    url=movie.get("url", ""),
                )
            )
    return results


def search_yts(query: str) -> list[TorrentResult]:
    """Try each YTS mirror until one responds."""
    for base in MIRRORS_YTS:
        try:
            results = _query_yts_mirror(base, query)
            if results:
                print(f"  YTS: connected via {base} ({len(results)} results)")
            return results
        except requests.RequestException:
            continue
    print("  YTS: all mirrors failed")
    return []


# ── apibay (The Pirate Bay JSON API) search ─────────────────────────────────


def _query_apibay_mirror(base: str, query: str) -> list[TorrentResult]:
    """Query a single apibay mirror. Infohashes are embedded in the response,
    so the magnet link is built directly — no second request needed."""
    url = f"{base}/q.php"
    resp = _get(
        url,
        params={"q": query, "cat": "200"},  # cat 200 = Video
        timeout=min(_TIMEOUT, _APIBAY_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()

    results: list[TorrentResult] = []
    for item in data:
        # apibay returns a single sentinel row when there are no matches.
        if item.get("id") in ("0", 0) or item.get("name") == "No results returned":
            continue
        infohash = item.get("info_hash", "")
        if not infohash or set(infohash) == {"0"}:
            continue
        name = item.get("name", "")
        results.append(
            TorrentResult(
                title=name,
                seeds=int(item.get("seeders", 0) or 0),
                peers=int(item.get("leechers", 0) or 0),
                size=_human_size(item.get("size")),
                magnet=_build_magnet(infohash, name),
                source="TPB",
            )
        )
    return results


def search_apibay(query: str) -> list[TorrentResult]:
    """Try each apibay mirror until one responds."""
    for base in MIRRORS_APIBAY:
        for _ in range(_APIBAY_ATTEMPTS):
            try:
                results = _query_apibay_mirror(base, query)
                if results:
                    print(f"  TPB: connected via {base} ({len(results)} results)")
                return results
            except (requests.RequestException, ValueError) as e:
                print(f"  TPB: {base} — {e}")
                continue
    print("  TPB: all mirrors failed")
    return []


# ── BitSearch API (particularly strong for Indian/regional releases) ───────


def search_bitsearch(query: str) -> list[TorrentResult]:
    """Search BitSearch's first-party API and construct magnets locally."""
    cache_key = re.sub(r"\s+", " ", query.casefold()).strip()
    with _BITSEARCH_CACHE_LOCK:
        cached = _BITSEARCH_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])
    headers = {**HEADERS, "Accept": "application/json"}
    api_key = os.environ.get("POPCORN_BITSEARCH_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    try:
        resp = _get(
            BITSEARCH_API,
            params={"q": query, "limit": 50},
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  BitSearch: {exc}")
        return []

    results: list[TorrentResult] = []
    for item in payload.get("results", []):
        infohash = str(item.get("infohash", "")).strip().upper()
        title = str(item.get("title", "")).strip()
        if not title or not re.fullmatch(r"[A-F0-9]{40}", infohash):
            continue
        try:
            seeds = max(0, int(item.get("seeders", 0) or 0))
            peers = max(0, int(item.get("leechers", 0) or 0))
        except (TypeError, ValueError):
            seeds = peers = 0
        results.append(
            TorrentResult(
                title=title,
                seeds=seeds,
                peers=peers,
                size=_human_size(item.get("size")),
                magnet=_build_magnet(infohash, title),
                source="BitSearch",
                url=f"https://bitsearch.to/torrents/{item.get('id', '')}",
            )
        )
    if results:
        print(f"  BitSearch: {len(results)} results")
    with _BITSEARCH_CACHE_LOCK:
        _BITSEARCH_CACHE[cache_key] = (time.monotonic() + 600, list(results))
    return results


# ── Torrents-CSV search ─────────────────────────────────────────────────────


def search_torrents_csv(query: str) -> list[TorrentResult]:
    """Search Torrents-CSV's JSON API.

    Like BitSearch, every row carries its own infohash, so magnets are built
    locally and no second request per result is needed.
    """
    try:
        resp = _get(TORRENTS_CSV_API, params={"q": query, "size": 50})
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  Torrents-CSV: {exc}")
        return []

    results: list[TorrentResult] = []
    for item in payload.get("torrents", []):
        infohash = str(item.get("infohash", "")).strip().upper()
        title = str(item.get("name", "")).strip()
        if not title or not re.fullmatch(r"[A-F0-9]{40}", infohash):
            continue
        results.append(
            TorrentResult(
                title=title,
                seeds=max(0, _int_or_zero(item.get("seeders"))),
                peers=max(0, _int_or_zero(item.get("leechers"))),
                size=_human_size(item.get("size_bytes")),
                magnet=_build_magnet(infohash, title),
                source="Torrents-CSV",
            )
        )
    if results:
        print(f"  Torrents-CSV: {len(results)} results")
    return results


# ── TorrentGalaxy search ────────────────────────────────────────────────────

# torrentgalaxy.to no longer resolves and .mx is usually a 522 origin error,
# so the working .one mirror is tried first and the rest stay as fallbacks.
MIRRORS_TG = [
    "https://torrentgalaxy.one",
    "https://torrentgalaxy.mx",
]


def _scrape_tg(base: str, query: str) -> list[TorrentResult]:
    """Scrape a single TorrentGalaxy mirror."""
    # The mirror retired /torrents.php?search= (it now bounces to the homepage,
    # which made every query return the same latest-torrents listing); search
    # lives at /get-posts/keywords:<query>, as on the rarbg mirrors. The term is
    # a path segment, so spaces must be %20 — "+" matches nothing there.
    url = f"{base}/get-posts/keywords:{quote(query, safe='')}"
    resp = _get(url)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[TorrentResult] = []

    for div in soup.select("div.tgxtable div.tgxtablerow"):
        # title
        title_cell = div.select_one("div.tgxtablecell.clickable-row")
        if not title_cell:
            continue
        link = title_cell.find("a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")

        # stats row
        cells = div.select("div.tgxtablecell")
        if len(cells) < 11:
            continue

        # This layout carries health as "[ seeds / peers ]"; size sits two
        # cells to its left. The old seeded/total columns are gone.
        health = re.findall(r"\d+", cells[10].get_text(strip=True))
        seeds = int(health[0]) if health else 0
        peers = int(health[1]) if len(health) > 1 else 0

        size = cells[7].get_text(strip=True) or "?"
        full_url = base + href if href.startswith("/") else href

        results.append(
            TorrentResult(
                title=title,
                seeds=seeds,
                peers=peers,
                size=size,
                source="TGx",
                url=full_url,
            )
        )

    return results


def search_tg(query: str) -> list[TorrentResult]:
    """Try each TorrentGalaxy mirror until one responds."""
    for base in MIRRORS_TG:
        try:
            results = _scrape_tg(base, query)
            if results:
                print(f"  TGx: connected via {base} ({len(results)} results)")
            return results
        except requests.RequestException as e:
            print(f"  TGx: {base} — {e}")
            continue
    print("  TGx: all mirrors failed")
    return []


def fetch_tg_magnet(torrent_url: str) -> str | None:
    """Visit a TorrentGalaxy torrent page and extract the magnet link."""
    try:
        resp = _get(torrent_url)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    magnet = soup.select_one("a[href^='magnet:']")
    if magnet:
        return magnet["href"]
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("magnet:"):
            return a["href"]
    return None


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
    """Return a magnet URI for a result, fetching if necessary."""
    if r.magnet and r.magnet.startswith("magnet:"):
        return r.magnet

    if r.source == "yts" and r.magnet:
        return _build_magnet(r.magnet, r.title)

    if r.source == "RARBG" and r.url:
        return fetch_rarbg_magnet(r.url)

    if r.source == "TGx" and r.url:
        return fetch_tg_magnet(r.url)

    return None


ARIA2_FLAGS = [
    "--seed-time=0",
    # ── speed tuning ──
    "--max-overall-download-limit=0",     # no cap
    "--max-download-limit=0",             # no per-download cap either
    "--bt-max-peers=200",                 # fast swarms without exhausting the host
    "--bt-request-peer-speed-limit=50M",  # keep pulling peers until 50M/s
    "--disk-cache=64M",                   # smooth bursty peer and disk throughput
    "--continue=true",                    # reuse verified pieces after an interruption
    "--optimize-concurrent-downloads=true",
    "--enable-dht=true",
    "--enable-peer-exchange=true",
    "--bt-enable-lpd=true",
    "--file-allocation=falloc",           # instant prealloc on ext4/xfs
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

    print(f"\n  Launching aria2c...\n")
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
        choices=["tpb", "regional", "rarbg", "yts", "tgx", "torrents-csv", "all"],
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
    _TIMEOUT = args.timeout

    if args.doh:
        socket.getaddrinfo = _doh_getaddrinfo
        print("Using DNS-over-HTTPS (Cloudflare) for hostname resolution.\n")

    if not _check_internet():
        if args.proxy:
            print("Warning: connectivity check failed — proxy may not be running.")
            print("Continuing anyway (torrent sources may still be reachable).\n")
        else:
            print("No internet connectivity — cannot reach external hosts.")
            print("Check DNS, firewall, or proxy settings.")
            sys.exit(1)

    all_results: list[TorrentResult] = []

    if args.source in ("tpb", "all"):
        print(f'Searching The Pirate Bay for "{query}"...')
        results_tpb = search_apibay(query)
        all_results.extend(results_tpb)
        print(f"  Found {len(results_tpb)} results.\n")

    if args.source in ("regional", "all"):
        print(f'Searching BitSearch for "{query}"...')
        results_bitsearch = search_bitsearch(query)
        all_results.extend(results_bitsearch)
        print(f"  Found {len(results_bitsearch)} results.\n")

    if args.source in ("rarbg", "all"):
        print(f'Searching RARBG for "{query}"...')
        results_rarbg = search_rarbg(query)
        all_results.extend(results_rarbg)
        print(f"  Found {len(results_rarbg)} results.\n")

    if args.source in ("yts", "all"):
        print(f'Searching YTS for "{query}"...')
        results_yts = search_yts(query)
        all_results.extend(results_yts)
        print(f"  Found {len(results_yts)} results.\n")

    if args.source in ("tgx", "all"):
        print(f'Searching TorrentGalaxy for "{query}"...')
        results_tg = search_tg(query)
        all_results.extend(results_tg)
        print(f"  Found {len(results_tg)} results.\n")

    if args.source in ("torrents-csv", "all"):
        print(f'Searching Torrents-CSV for "{query}"...')
        results_csv = search_torrents_csv(query)
        all_results.extend(results_csv)
        print(f"  Found {len(results_csv)} results.\n")

    if not all_results:
        print("No results from any source.")
        if not args.proxy and not args.doh:
            print("Torrent sources appear to be blocked on this network.\n")
            print("Try DNS-over-HTTPS first (bypasses DNS blocking):")
            print("  python3 popcorn.py \"Inception\" --doh\n")
            print("Or route through a SOCKS/HTTP proxy:")
            print("  python3 popcorn.py \"Inception\" --proxy socks5h://127.0.0.1:9050")
        sys.exit(0)

    # sort by seeds descending
    all_results.sort(key=lambda r: r.seeds, reverse=True)
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
