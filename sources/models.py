"""Canonical release metadata and explicit, tunable ranking weights."""

import base64
import math
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit


def normalize_hash(value):
    value = str(value or "").strip().lower()
    if re.fullmatch(r"[a-f0-9]{40}", value) and set(value) != {"0"}:
        return value
    if re.fullmatch(r"[a-z2-7]{32}", value):
        return base64.b32decode(value.upper()).hex()
    return None


def size_bytes(value):
    if isinstance(value, (int, float)):
        return max(0, int(value))
    match = re.match(r"\s*([\d.]+)\s*([KMGT]?)(i?B)\b", str(value), re.I)
    if not match:
        return 0
    base = 1024 if match[3].lower() == "ib" else 1000
    return int(float(match[1]) * base ** " KMGT".index(match[2].upper()))


def title_key(value):
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


@dataclass
class Release:
    # The first seven fields preserve TorrentResult's construction API.
    title: str
    seeds: int = 0
    peers: int = 0
    size: str = "?"
    magnet: str | None = None
    source: str = ""
    url: str = ""
    infohash: str | None = None
    size_bytes: int = 0
    sources: list[str] = field(default_factory=list)
    trackers: list[str] = field(default_factory=list)
    resolution: str | None = None
    codec: str | None = None
    hdr: str | None = None
    audio: str | None = None
    language: str | None = None
    release_group: str | None = None
    media_type: str = "movie"
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    season_pack: bool = False
    source_type: str | None = None
    confidence: float = 0.7
    score: float = 0
    score_details: dict = field(default_factory=dict)

    def __post_init__(self):
        self.title = re.sub(r"\s+", " ", self.title).strip()
        self.seeds = max(0, int(self.seeds or 0))
        self.peers = max(0, int(self.peers or 0))
        params = parse_qs(urlsplit(self.magnet or "").query)
        hashes = [
            x.removeprefix("urn:btih:")
            for x in params.get("xt", [])
            if x.startswith("urn:btih:")
        ]
        self.infohash = normalize_hash(
            self.infohash or next(iter(hashes), None) or self.magnet
        )
        self.trackers = list(dict.fromkeys([*self.trackers, *params.get("tr", [])]))
        self.sources = sorted(set(self.sources or [self.source]))
        self.size_bytes = self.size_bytes or size_bytes(self.size)
        text = self.title.replace(".", " ").replace("_", " ")

        def tag(pattern):
            match = re.search(pattern, text, re.I)
            return match[0] if match else None

        self.resolution = self.resolution or tag(
            r"\b(?:2160p|1080p|1440p|720p|480p|576p|sd|4k|uhd)\b"
        )
        self.codec = self.codec or tag(r"\b(?:x26[45]|hevc|av1|h\s?26[45]|xvid|divx)\b")
        self.hdr = self.hdr or tag(r"\b(?:HDR10\+?|HDR|DV|Dolby Vision)\b")
        self.audio = self.audio or tag(
            r"\b(?:atmos|truehd|dts|ddp|eac3|ac3|aac)(?:\s*5\s*1)?\b"
        )
        self.language = self.language or tag(
            r"\b(?:English|Tamil|Telugu|Hindi|Malayalam|Kannada|Bengali|multi)\b"
        )
        self.source_type = self.source_type or tag(
            r"\b(?:remux|blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|hdrip|dvdrip|cam|hdcam|telesync)\b"
        )
        if self.resolution:
            self.resolution = (
                "4K"
                if self.resolution.casefold() in {"4k", "2160p", "uhd"}
                else self.resolution.casefold()
            )
            if self.resolution in {"576p", "sd"}:
                self.resolution = "480p"
        if self.codec:
            self.codec = {
                "x265": "HEVC",
                "h265": "HEVC",
                "hevc": "HEVC",
                "x264": "H.264",
                "h264": "H.264",
                "av1": "AV1",
                "xvid": "XviD",
                "divx": "XviD",
            }.get(self.codec.casefold().replace(" ", ""), self.codec)
        if self.source_type:
            self.source_type = {
                "bluray": "BluRay",
                "blu-ray": "BluRay",
                "bdrip": "BluRay",
                "brrip": "BluRay",
                "web-dl": "WEB-DL",
                "webdl": "WEB-DL",
                "webrip": "WEBRip",
                "hdrip": "HDRip",
                "hdtv": "HDRip",
                "dvdrip": "DVDRip",
                "remux": "Remux",
                "cam": "CAM",
                "hdcam": "CAM",
                "telesync": "TeleSync",
            }.get(self.source_type.casefold(), self.source_type)
        year = tag(r"\b(?:19|20)\d{2}\b")
        self.year = self.year or (int(year) if year else None)
        episode = re.search(
            r"\bS(\d{1,2})(?:\s*E(\d{1,3}))?\b|\bSeason\s*(\d{1,2})\b", text, re.I
        )
        if episode:
            self.season = int(episode[1] or episode[3])
            self.episode = int(episode[2]) if episode[2] else None
            self.season_pack = self.episode is None
            self.media_type = "episode" if self.episode is not None else "season"
        group = re.search(r"-([\w]+)$", self.title)
        self.release_group = self.release_group or (group[1] if group else None)


@dataclass(frozen=True)
class SearchContext:
    title: str
    year: int | None = None
    media_type: str | None = None
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    language: str | None = None
    codec: str | None = None
    aliases: tuple[str, ...] = ()

    def queries(self, limit=3):
        suffix = f" S{self.season:02d}" if self.season is not None else ""
        if self.episode is not None and suffix:
            suffix += f"E{self.episode:02d}"
        candidates = [self.title + suffix]
        if self.year and not suffix and str(self.year) not in self.title:
            candidates.insert(0, f"{self.title} {self.year}")
        candidates.extend(name + suffix for name in self.aliases)
        return list(dict.fromkeys(x.strip() for x in candidates if x.strip()))[:limit]


WEIGHTS = {
    "title": 60,
    "year": 20,
    "wrong_year": -35,
    "episode": 30,
    "wrong_episode": -80,
    "media": 15,
    "wrong_media": -35,
    "resolution": 15,
    "language": 12,
    "codec": 8,
    "seeds": 3,
    "confidence": 10,
    "corroboration": 4,
    "size": 5,
    "bad_size": -25,
    "poor_source": -25,
    "good_source": 5,
}


def rank(release, context):
    query = set(title_key(context.title).split())
    title = set(title_key(release.title).split())
    details = {
        "title": WEIGHTS["title"] * len(query & title) / max(1, len(query)),
        "seeds": WEIGHTS["seeds"] * min(6, math.log1p(release.seeds)),
        "confidence": WEIGHTS["confidence"] * release.confidence,
        "corroboration": WEIGHTS["corroboration"] * min(3, len(release.sources) - 1),
    }
    for attribute in ("year", "resolution", "language", "codec"):
        wanted, actual = getattr(context, attribute), getattr(release, attribute)
        if wanted and actual:
            wanted = (
                "4K"
                if attribute == "resolution" and str(wanted).casefold() == "2160p"
                else wanted
            )
            details[attribute] = (
                WEIGHTS[attribute]
                if str(wanted).casefold() == str(actual).casefold()
                else WEIGHTS.get("wrong_" + attribute, 0)
            )
    if context.media_type:
        match = (
            context.media_type == release.media_type
            or context.media_type == "tv"
            and release.media_type in {"season", "episode"}
        )
        details["media"] = WEIGHTS["media"] if match else WEIGHTS["wrong_media"]
    if context.season is not None:
        correct = release.season == context.season and (
            context.episode is None or release.episode == context.episode
        )
        details["episode"] = WEIGHTS["episode"] if correct else WEIGHTS["wrong_episode"]
    if release.size_bytes:
        details["size"] = (
            WEIGHTS["size"]
            if 80e6 <= release.size_bytes <= 150e9 or release.season_pack
            else WEIGHTS["bad_size"]
        )
    if release.source_type:
        details["source"] = (
            WEIGHTS["poor_source"]
            if release.source_type.casefold() in {"cam", "hdcam", "telesync"}
            else WEIGHTS["good_source"]
        )
    release.score_details = details
    release.score = round(sum(details.values()), 2)
    return release.score


def merge_releases(results):
    merged = {}
    for item in results:
        # A bare title is insufficient evidence when size/hash are missing.
        key = item.infohash or (
            title_key(item.title),
            item.size_bytes or item.size,
            item.source if not item.size_bytes else "",
        )
        if key not in merged:
            merged[key] = item
            continue
        existing = merged[key]
        existing.sources = sorted(set(existing.sources + item.sources))
        existing.trackers = list(dict.fromkeys(existing.trackers + item.trackers))
        existing.seeds = max(existing.seeds, item.seeds)
        existing.peers = max(existing.peers, item.peers)
        existing.confidence = max(existing.confidence, item.confidence)
        if existing.size in {"?", "0"} and item.size not in {"?", "0"}:
            existing.size = item.size
        for attribute in (
            "infohash",
            "magnet",
            "size_bytes",
            "resolution",
            "codec",
            "hdr",
            "audio",
            "language",
            "year",
            "release_group",
        ):
            if not getattr(existing, attribute):
                setattr(existing, attribute, getattr(item, attribute))
    return list(merged.values())
