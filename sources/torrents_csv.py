from .base import Provider, release_row, rows


class TorrentsCSV(Provider):
    id, name, priority = "torrents-csv", "Torrents-CSV", 20
    endpoints = ("https://torrents-csv.com/service/search",)

    def fetch(self, endpoint, query, context, deadline):
        payload = self.request(endpoint, deadline, params={"q": query, "size": 50})
        return [
            release_row(
                r.get("name"),
                r.get("infohash"),
                self.id,
                r.get("seeders", 0),
                r.get("leechers", 0),
                r.get("size_bytes", 0),
            )
            for r in rows(payload, "torrents")
        ]
