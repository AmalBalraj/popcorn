from urllib.parse import quote_plus, urljoin
from .base import Provider, ProviderError
from .models import Release


class RARBG(Provider):
    id, name, priority = "rarbg", "theRARBG", 50
    endpoints = ("https://therarbg.com",)
    json_response = False

    def fetch(self, endpoint, query, context, deadline):
        found = []
        categories = (
            ("TV",)
            if context.media_type in {"tv", "episode", "season"}
            else ("Movies",)
            if context.media_type == "movie"
            else ("Movies", "TV")
        )
        for category in categories:
            soup = self.request(
                f"{endpoint}/get-posts/keywords:{quote_plus(query)}:category:{category}/",
                deadline,
            )
            entries = soup.select("tr.list-entry")
            if not entries and not any(
                marker in soup.get_text().lower()
                for marker in ("no results", "no posts", "no torrents")
            ):
                raise ProviderError("schema")
            for row in entries:
                link, size = (
                    row.select_one("td.cellName a"),
                    row.select_one("td.sizeCell"),
                )
                if not link or not link.get("href"):
                    raise ProviderError("schema")

                def count(selector):
                    cell = row.select_one(selector)
                    return int(cell.get_text(strip=True) or 0) if cell else 0

                found.append(
                    Release(
                        link.get_text(strip=True),
                        count("td[style*='color: green']"),
                        count("td[style*='color: red']"),
                        size.get_text(strip=True) if size else "?",
                        source=self.id,
                        url=urljoin(endpoint, link["href"]),
                        confidence=0.5,
                    )
                )
        return found
