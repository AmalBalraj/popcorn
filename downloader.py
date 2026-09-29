"""Downloader-owned tracker policy; no index credentials or parser logic."""

import os
from urllib.parse import parse_qs, quote_plus, urlsplit
from sources.models import normalize_hash

DEFAULT_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
)


def build_magnet(infohash, name, trackers=()):
    infohash = normalize_hash(infohash)
    if not infohash:
        raise ValueError("Invalid torrent infohash")
    configured = os.environ.get("POPCORN_TRACKERS")
    defaults = configured.split(",") if configured is not None else DEFAULT_TRACKERS
    combined = dict.fromkeys(t.strip() for t in [*trackers, *defaults] if t.strip())
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={quote_plus(name)}"
    return magnet + "".join(
        f"&tr={quote_plus(t)}"
        for t in combined
        if urlsplit(t).scheme in {"udp", "http", "https"}
    )


def magnet_for_release(release):
    if release.infohash:
        return build_magnet(release.infohash, release.title, release.trackers)
    if release.magnet:
        params = parse_qs(urlsplit(release.magnet).query)
        for xt in params.get("xt", []):
            if xt.startswith("urn:btih:"):
                return build_magnet(
                    xt.removeprefix("urn:btih:"), release.title, params.get("tr", [])
                )
    return None
