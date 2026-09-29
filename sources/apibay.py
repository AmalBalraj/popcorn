from .base import Provider, release_row, rows


class APiBay(Provider):
    id, name, priority = "tpb", "The Pirate Bay", 30
    endpoints = ("https://apibay.org",)

    def fetch(self, endpoint, query, context, deadline):
        payload = self.request(
            endpoint + "/q.php", deadline, params={"q": query, "cat": "200"}
        )
        return [
            release_row(
                r.get("name"),
                r.get("info_hash"),
                self.id,
                r.get("seeders", 0),
                r.get("leechers", 0),
                int(r.get("size", 0)),
                media_type="tv" if str(r.get("category", "")) in {"205", "208"} else "movie",
            )
            for r in rows(payload)
            if r.get("id") not in {"0", 0} and r.get("name") != "No results returned"
        ]
