import os
from .base import Provider, release_row, rows


class BitSearch(Provider):
    id, name, priority = "regional", "BitSearch", 10
    endpoints = ("https://bitsearch.eu/api/v1/search",)

    def fetch(self, endpoint, query, context, deadline):
        key = os.environ.get("POPCORN_BITSEARCH_API_KEY", "").strip()
        payload = self.request(
            endpoint,
            deadline,
            params={"q": query, "limit": 50},
            headers={"x-api-key": key} if key else {},
        )
        return [
            release_row(
                r.get("title"),
                r.get("infohash"),
                self.id,
                r.get("seeders", 0),
                r.get("leechers", 0),
                r.get("size", 0),
            )
            for r in rows(payload, "results")
        ]
