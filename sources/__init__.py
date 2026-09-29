"""Single registry shared by CLI and web; adding an adapter requires one import."""

from .apibay import APiBay
from .bitsearch import BitSearch
from .config import Config
from .models import Release as Release, SearchContext as SearchContext
from .rarbg import RARBG
from .service import SearchService
from .torrentgalaxy import TorrentGalaxy
from .torrents_csv import TorrentsCSV
from .torznab import Torznab
from .yts import YTS

config = Config.from_env()
providers = [
    kind(config) for kind in (BitSearch, APiBay, RARBG, YTS, TorrentGalaxy, TorrentsCSV)
]
if config.providers.get("torznab", {}).get("endpoints"):
    providers.append(Torznab(config))
service = SearchService(providers, config)
