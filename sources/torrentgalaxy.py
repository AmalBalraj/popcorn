import re
from urllib.parse import quote, urljoin
from .base import Provider, ProviderError
from .models import Release


class TorrentGalaxy(Provider):
    id, name, priority = "tgx", "TorrentGalaxy", 60
    endpoints = ("https://torrentgalaxy.one", "https://torrentgalaxy.mx")
    json_response = False

    def fetch(self, endpoint, query, context, deadline):
        soup = self.request(
            f"{endpoint}/get-posts/keywords:{quote(query, safe='')}", deadline
        )
        entries = soup.select("div.tgxtable div.tgxtablerow")
        if (
            not entries
            and not soup.select("div.tgxtable")
            and "no results" not in soup.get_text().lower()
        ):
            raise ProviderError("schema")
        found = []
        for row in entries:
            link = row.select_one("div.tgxtablecell.clickable-row a")
            cells = row.select("div.tgxtablecell")
            if not link or not link.get("href") or len(cells) < 11:
                raise ProviderError("schema")
            counts = re.findall(r"\d+", cells[10].get_text())
            found.append(
                Release(
                    link.get_text(strip=True),
                    int(counts[0]) if counts else 0,
                    int(counts[1]) if len(counts) > 1 else 0,
                    cells[7].get_text(strip=True),
                    source=self.id,
                    url=urljoin(endpoint, link["href"]),
                    confidence=0.5,
                )
            )
        return found
