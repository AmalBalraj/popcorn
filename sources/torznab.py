"""Optional operator-configured Torznab API (Jackett/Prowlarr compatible)."""

import os
import xml.etree.ElementTree as ET
from .base import Provider, ProviderError, release_row


class Torznab(Provider):
    id, name, priority = "torznab", "Configured Torznab", 5
    json_response = False

    def fetch(self, endpoint, query, context, deadline):
        body = self.request(
            endpoint,
            deadline,
            params={
                "t": "search",
                "q": query,
                "cat": "2000,5000",
                "limit": 100,
                "apikey": os.environ.get("POPCORN_TORZNAB_API_KEY", ""),
            },
            raw=True,
        )
        if "<!DOCTYPE" in body or "<!ENTITY" in body:
            raise ProviderError("invalid_payload")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ProviderError("schema") from exc
        if root.tag != "rss" or root.find("channel") is None:
            raise ProviderError("schema")
        found = []
        for item in root.findall("channel/item"):
            attrs = {
                child.get("name"): child.get("value")
                for child in item
                if child.tag.endswith("}attr")
            }
            link = item.findtext("link", "")
            magnet = attrs.get("magneturl") or (
                link if link.startswith("magnet:") else None
            )
            # Torrent-file-only results require a different downloader contract.
            if not attrs.get("infohash") and not magnet:
                continue
            found.append(
                release_row(
                    item.findtext("title"),
                    attrs.get("infohash"),
                    self.id,
                    attrs.get("seeders", 0),
                    attrs.get("peers", 0),
                    int(item.findtext("size", "0")),
                    magnet=magnet,
                    confidence=0.9,
                )
            )
        return found
