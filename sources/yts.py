from .base import Provider, ProviderError, release_row, rows


class YTS(Provider):
    id, name, priority = "yts", "YTS", 40
    endpoints = (
        "https://movies-api.accel.li",
        "https://yts.lt",
        "https://yts.bz",
        "https://yts.rs",
        "https://yts.do",
    )

    def fetch(self, endpoint, query, context, deadline):
        payload = self.request(
            endpoint + "/api/v2/list_movies.json",
            deadline,
            params={"query_term": query, "limit": 20},
        )
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or not isinstance(payload.get("data"), dict)
        ):
            raise ProviderError("schema")
        data = payload["data"]
        if data.get("movie_count") == 0:
            return []
        found = []
        for movie in rows(data, "movies"):
            for torrent in rows(movie, "torrents"):
                title = f"{movie['title']} ({movie['year']}) — {torrent['quality']} {torrent['type']}"
                found.append(
                    release_row(
                        title,
                        torrent.get("hash"),
                        self.id,
                        torrent.get("seeds", 0),
                        torrent.get("peers", 0),
                        torrent.get("size_bytes") or torrent.get("size", "?"),
                        confidence=0.9,
                    )
                )
        return found
