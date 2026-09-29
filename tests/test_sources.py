import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from downloader import build_magnet
from sources.base import Provider, ProviderError
from sources.config import Config
from sources.models import Release, SearchContext, merge_releases, rank
from sources.service import SearchService
from sources.apibay import APiBay
from sources.bitsearch import BitSearch
from sources.yts import YTS
from sources.torrents_csv import TorrentsCSV
from sources.rarbg import RARBG
from sources.torrentgalaxy import TorrentGalaxy
from sources.torznab import Torznab

HASH = "a" * 40


class FakeProvider(Provider):
    endpoints = ("https://example.invalid",)

    def __init__(self, name, config, behavior=None):
        self.id = self.name = name
        self.calls = 0
        self.behavior = behavior
        super().__init__(config)

    def fetch(self, endpoint, query, context, deadline):
        self.calls += 1
        if self.behavior:
            return self.behavior()
        return [
            Release(
                "Example 2020 1080p WEB-DL",
                20,
                size="1 GiB",
                source=self.id,
                infohash=HASH,
            )
        ]


@pytest.fixture
def config():
    return Config(
        retries=0,
        search_deadline=0.3,
        provider_timeout=0.25,
        early_return=0.04,
        min_results=1,
        cooldown=0.02,
        cache_ttl=0.02,
        stale_ttl=10,
        variants=1,
    )


@pytest.fixture
def services():
    created = []

    def make(providers, config):
        service = SearchService(providers, config)
        created.append(service)
        return service

    yield make
    for service in created:
        service.executor.shutdown(wait=True)


def fail(reason, **kwargs):
    def run():
        raise ProviderError(reason, **kwargs)

    return run


def test_scenario_a_six_providers_return_without_stragglers(config, services):
    def timeout():
        time.sleep(0.2)
        raise ProviderError("timeout", retryable=True)

    providers = [
        FakeProvider("healthy-1", config),
        FakeProvider("healthy-2", config),
        FakeProvider("timeout-1", config, timeout),
        FakeProvider("timeout-2", config, timeout),
        FakeProvider("json", config, fail("invalid_json")),
        FakeProvider("503", config, fail("http", status=503, retryable=True)),
    ]
    service = services(providers, config)
    start = time.monotonic()
    result = service.search(SearchContext("Example"))
    assert time.monotonic() - start < 0.15
    assert len(result.releases) == 1
    assert result.releases[0].sources == ["healthy-1", "healthy-2"]


def test_scenario_b_three_providers_merge_hash_metadata(config, services):
    result = services([FakeProvider(str(n), config) for n in range(3)], config).search(
        SearchContext("Example")
    )
    assert len(result.releases) == 1
    assert result.releases[0].sources == ["0", "1", "2"]


def test_scenarios_c_d_breaker_and_recovery(config, services):
    provider = FakeProvider("bad", config, fail("timeout"))
    service = services([provider], config)
    for n in range(config.failure_threshold):
        service.search(SearchContext(f"Example {n}"), refresh=True)
    assert provider.health.snapshot()["state"] == "OPEN"
    calls = provider.calls
    start = time.monotonic()
    assert not service.search(SearchContext("Skipped"), refresh=True).releases
    assert time.monotonic() - start < 0.05
    assert provider.calls == calls
    time.sleep(config.cooldown + 0.01)
    provider.behavior = None
    assert service.search(SearchContext("Recovered"), refresh=True).releases
    assert provider.health.snapshot()["state"] == "HEALTHY"
    assert provider.health.consecutive_failures == 0


def test_scenario_e_stale_cache_when_all_live_fail(config, services):
    provider = FakeProvider("only", config)
    service = services([provider], config)
    context = SearchContext("Example")
    assert service.search(context).releases
    time.sleep(config.cache_ttl + 0.01)
    provider.behavior = fail("http", status=503)
    result = service.search(context)
    assert result.releases and result.stale


def test_scenario_f_unexpected_parser_exception(config, services):
    def broken():
        raise IndexError("unexpected parser shape")

    bad, good = FakeProvider("bad", config, broken), FakeProvider("good", config)
    result = services([bad, good], config).search(SearchContext("Example"))
    assert result.releases[0].sources == ["good"]
    assert bad.health.malformed_failures == 1


def test_query_cache_copies_and_context_isolation(config, services):
    provider = FakeProvider("p", config)
    service = services([provider], config)
    context = SearchContext("Example", year=2020)
    first = service.search(context)
    first.releases[0].sources.append("mutation")
    cached = service.search(context)
    assert cached.cache_hit and cached.releases[0].sources == ["p"]
    assert provider.calls == 1
    service.search(SearchContext("Example", year=2021))
    assert provider.calls == 2


def test_expired_stale_cache_is_not_returned(config, services):
    provider = FakeProvider("p", config)
    service = services([provider], config)
    context = SearchContext("Example")
    service.search(context)
    service.cache[next(iter(service.cache))] = (time.monotonic() - 20, [Release("Old")])
    provider.behavior = fail("timeout")
    assert not service.search(context, refresh=True).releases


def test_worker_concurrency_is_bounded_and_all_providers_get_turn(config, services):
    config.concurrency = 2
    config.min_results = 100
    active = peak = 0
    lock = threading.Lock()

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return []

    providers = [FakeProvider(str(n), config, work) for n in range(6)]
    services(providers, config).search(SearchContext("Example"))
    assert peak <= 2
    assert all(p.calls == 1 for p in providers)


def test_parallel_searches_do_not_duplicate_inflight_provider(config, services):
    config.early_return = 0.005
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.08)
        return []

    p = FakeProvider("slow", config, slow)
    service = services([p], config)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.search, SearchContext("one"))
        assert started.wait(1)
        second = service.search(SearchContext("two"))
        assert second.providers["slow"]["error"] == "in_flight"
        first.result()
    assert p.calls == 1


class Response:
    def __init__(
        self,
        body,
        status=200,
        content_type="application/json",
        retry_after="0",
        url="https://example.invalid/search",
    ):
        self.body = body.encode()
        self.status_code = status
        self.headers = {"Content-Type": content_type, "Retry-After": retry_after}
        self.encoding, self.url = "utf-8", url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, chunk):
        yield self.body


def http(monkeypatch, response):
    def get(*args, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("sources.base.fetch_response", get)


@pytest.mark.parametrize(
    "response,reason",
    [
        (Response("<html>Challenge</html>", content_type="text/html"), "content_type"),
        (Response("bad JSON"), "invalid_json"),
        (Response(""), "invalid_payload"),
        (Response("Just a moment cf-chl-"), "challenge"),
        (Response("{}", status=503), "http"),
        (requests.Timeout(), "timeout"),
        (requests.ConnectionError(), "network"),
        (requests.TooManyRedirects(), "redirect_loop"),
        (requests.exceptions.SSLError(), "tls"),
    ],
)
def test_http_response_validation(monkeypatch, config, response, reason):
    http(monkeypatch, response)
    with pytest.raises(ProviderError, match=reason):
        FakeProvider("test", config).request(
            "https://example.invalid/search", time.monotonic() + 1
        )


@pytest.mark.parametrize(
    "kind,payload",
    [
        (
            APiBay,
            [{"id": "1", "name": "Example", "info_hash": HASH, "size": "1000000000"}],
        ),
        (
            BitSearch,
            {"results": [{"title": "Example", "infohash": HASH, "size": 1000000000}]},
        ),
        (
            TorrentsCSV,
            {
                "torrents": [
                    {"name": "Example", "infohash": HASH, "size_bytes": 1000000000}
                ]
            },
        ),
        (
            YTS,
            {
                "status": "ok",
                "data": {
                    "movies": [
                        {
                            "title": "Example",
                            "year": 2020,
                            "torrents": [
                                {
                                    "hash": HASH,
                                    "quality": "1080p",
                                    "type": "bluray",
                                    "size_bytes": 1000000000,
                                }
                            ],
                        }
                    ]
                },
            },
        ),
    ],
)
def test_api_adapters_validate_and_normalize(monkeypatch, config, kind, payload):
    http(monkeypatch, Response(json.dumps(payload)))
    provider = kind(config)
    result = provider.search(
        "Example", SearchContext("Example"), time.monotonic() + 1, "id"
    )
    assert result[0].infohash == HASH and result[0].size_bytes == 1000000000
    http(monkeypatch, Response('{"changed_schema":true}'))
    with pytest.raises(ProviderError, match="schema"):
        provider.search("Example", SearchContext("Example"), time.monotonic() + 1, "id")
    assert provider.health.malformed_failures == 1


def test_rate_limit_and_no_permanent_retry(monkeypatch, config):
    config.retries = 3
    http(monkeypatch, Response("{}", status=429, retry_after="60"))
    p = FakeProvider("p", config)
    p.fetch = lambda e, q, c, d: p.request(e, d)
    with pytest.raises(ProviderError):
        p.search("Example", SearchContext("Example"), time.monotonic() + 1, "id")
    assert p.health.snapshot()["state"] == "OPEN"
    assert p.health.rate_limit_until > time.time() + 50


def test_mirror_failover_health_and_valid_empty(config):
    config.providers = {
        "p": {"endpoints": ["https://bad.invalid", "https://good.invalid"]}
    }
    p = FakeProvider("p", config)
    calls = []

    def fetch(endpoint, *args):
        calls.append(endpoint)
        if "bad" in endpoint:
            raise ProviderError("timeout")
        return []  # An empty, valid response is a success.

    p.fetch = fetch
    assert (
        p.search("Example", SearchContext("Example"), time.monotonic() + 1, "id") == []
    )
    assert p.health.success_count == 1
    p.search("Example", SearchContext("Example"), time.monotonic() + 1, "id")
    assert calls == [
        "https://bad.invalid",
        "https://good.invalid",
        "https://good.invalid",
    ]


def test_local_magnets_base32_and_trackers(monkeypatch):
    encoded = base64.b32encode(bytes.fromhex(HASH)).decode()
    release = Release("Example", magnet=f"magnet:?xt=urn:btih:{encoded}")
    assert release.infohash == HASH
    monkeypatch.setenv(
        "POPCORN_TRACKERS", "udp://a/announce,udp://a/announce,https://b/announce"
    )
    magnet = build_magnet(HASH, "Example", ["udp://a/announce"])
    assert magnet.count("&tr=") == 2
    assert "urn:btih:" + HASH in magnet
    with pytest.raises(ValueError):
        build_magnet("invalid", "Example")


def test_ranking_correct_identity_beats_exaggerated_seed_count():
    context = SearchContext(
        "Example", year=2020, media_type="movie", resolution="1080p"
    )
    correct = Release("Example 2020 1080p WEB-DL", seeds=20, size="2 GiB")
    wrong = Release("Other 1999 2160p CAM", seeds=9999999, size="20 MiB")
    assert rank(correct, context) > rank(wrong, context)
    assert "title" in correct.score_details
    episode_context = SearchContext(
        "Example", season=2, episode=3, media_type="episode"
    )
    assert rank(Release("Example S02E03 1080p"), episode_context) > rank(
        Release("Example S01E01 1080p", seeds=999999), episode_context
    )


def test_release_fields_and_compact_queries():
    r = Release(
        "Example.2020.S02E03.2160p.HEVC.HDR10.Atmos.English-GROUP", size="1 GiB"
    )
    assert (r.year, r.season, r.episode, r.media_type) == (2020, 2, 3, "episode")
    assert r.hdr == "HDR10" and r.codec == "HEVC" and r.release_group == "GROUP"
    assert r.size_bytes == 1024**3
    assert SearchContext(
        "Example", season=2, episode=3, aliases=("Original",)
    ).queries() == ["Example S02E03", "Original S02E03"]


def test_unhashed_dedup_is_conservative():
    results = merge_releases(
        [
            Release("Example", size="1 GiB", source="a"),
            Release("Example", size="2 GiB", source="b"),
            Release("Unknown", source="a"),
            Release("Unknown", source="b"),
        ]
    )
    assert len(results) == 4


def test_torznab_parser_and_credential_redaction(monkeypatch, config):
    config.providers = {
        "torznab": {
            "endpoints": [
                "https://user:secret@example.invalid/private-token/api?apikey=secret"
            ]
        }
    }
    p = Torznab(config)
    body = f'<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item><title>Example</title><size>1000000000</size><torznab:attr name="infohash" value="{HASH}"/><torznab:attr name="seeders" value="2"/></item></channel></rss>'
    http(monkeypatch, Response(body, content_type="application/xml"))
    assert (
        p.search("Example", SearchContext("Example"), time.monotonic() + 1, "id")[
            0
        ].infohash
        == HASH
    )
    assert "secret" not in json.dumps(p.diagnostics())
    assert "private-token" not in json.dumps(p.diagnostics())


@pytest.mark.parametrize(
    "kind,body",
    [
        (
            RARBG,
            '<table><tr class="list-entry"><td class="cellName"><a href="/detail">Example</a></td><td class="sizeCell">1 GiB</td><td style="color: green">4</td></tr></table>',
        ),
        (
            TorrentGalaxy,
            '<div class="tgxtable"><div class="tgxtablerow"><div class="tgxtablecell clickable-row"><a href="/detail">Example</a></div>'
            + '<div class="tgxtablecell"></div>' * 6
            + '<div class="tgxtablecell">1 GiB</div>'
            + '<div class="tgxtablecell"></div>' * 2
            + '<div class="tgxtablecell">[4 / 2]</div></div></div>',
        ),
    ],
)
def test_html_adapters_and_placeholder_rejection(monkeypatch, config, kind, body):
    http(monkeypatch, Response(body, content_type="text/html"))
    p = kind(config)
    result = p.search(
        "Example",
        SearchContext("Example", media_type="movie"),
        time.monotonic() + 1,
        "id",
    )
    assert result[0].seeds == 4 and result[0].title == "Example"
    http(monkeypatch, Response("<html>Coming soon</html>", content_type="text/html"))
    with pytest.raises(ProviderError, match="schema"):
        p.search("Example", SearchContext("Example"), time.monotonic() + 1, "id")


def test_retryable_errors_retry_but_permanent_errors_do_not(config):
    config.retries = 1
    config.retry_backoff = 0.001
    p = FakeProvider("p", config)
    calls = []

    def transient(*args):
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("http", status=503, retryable=True)
        return []

    p.fetch = transient
    assert (
        p.search("Example", SearchContext("Example"), time.monotonic() + 1, "id") == []
    )
    assert len(calls) == 2
    calls.clear()

    def permanent(*args):
        calls.append(1)
        raise ProviderError("http", status=403)

    p.fetch = permanent
    with pytest.raises(ProviderError):
        p.search("Example", SearchContext("Example"), time.monotonic() + 1, "id")
    assert len(calls) == 1


def test_failed_query_cache_suppresses_identical_requests(config, services):
    p = FakeProvider("p", config, fail("timeout"))
    service = services([p], config)
    context = SearchContext("Example")
    service.search(context)
    second = service.search(context)
    assert p.calls == 1 and second.providers["p"]["cache_hit"]


def test_half_open_lease_allows_one_probe(config):
    from sources.health import Health

    config.failure_threshold = 1
    health = Health(config)
    health.failure(ProviderError("timeout"), 0.1)
    assert not health.acquire()
    time.sleep(config.cooldown + 0.01)
    assert health.acquire()
    assert not health.acquire()
    health.success(0.01)
    assert health.snapshot()["state"] == "HEALTHY"


def test_mirror_recovers_on_cached_query_without_blocking(config, services):
    config.cache_ttl = 1
    config.providers = {
        "p": {"endpoints": ["https://bad.invalid", "https://good.invalid"]}
    }
    p = FakeProvider("p", config)
    recovering = False

    def fetch(endpoint, *args):
        if "bad" in endpoint and not recovering:
            raise ProviderError("timeout")
        return [Release("Example", infohash=HASH, source="p")]

    p.fetch = fetch
    service = services([p], config)
    context = SearchContext("Example")
    service.search(context)
    time.sleep(config.cooldown + 0.01)
    recovering = True
    assert service.search(context).cache_hit
    deadline = time.monotonic() + 1
    while (
        p.mirrors["https://bad.invalid"].snapshot()["state"] != "HEALTHY"
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert p.mirrors["https://bad.invalid"].snapshot()["state"] == "HEALTHY"


def test_environment_decimal_timeouts_and_invalid_config(monkeypatch):
    monkeypatch.setenv("POPCORN_SOURCE_CONNECT_TIMEOUT", "0.75")
    assert Config.from_env().connect_timeout == 0.75
    monkeypatch.setenv("POPCORN_SOURCE_CONNECT_TIMEOUT", "nan")
    with pytest.raises(ValueError):
        Config.from_env()
    monkeypatch.delenv("POPCORN_SOURCE_CONNECT_TIMEOUT")
    monkeypatch.setenv("POPCORN_SOURCE_PROVIDERS", '{"tpb":{"enabled":"false"}}')
    with pytest.raises(ValueError):
        Config.from_env()


def test_hash_resolution_never_makes_http_request(config, services, monkeypatch):
    p = FakeProvider("p", config)

    def unexpected(*args, **kwargs):
        pytest.fail("infohash resolution must not call an endpoint")

    monkeypatch.setattr("sources.base.fetch_response", unexpected)
    release = Release("Example", infohash=HASH, source="p")
    assert services([p], config).resolve(release) is release


def test_detail_parser_failure_updates_health(config, monkeypatch):
    p = RARBG(config)
    http(
        monkeypatch,
        Response("<html>No magnet available</html>", content_type="text/html"),
    )
    with pytest.raises(ProviderError, match="schema"):
        p.resolve(Release("Example", source="rarbg", url="https://therarbg.com/detail"))
    assert p.health.malformed_failures == 1


def test_dead_mirror_cannot_consume_healthy_mirror_budget(config):
    config.providers = {
        "p": {"endpoints": ["https://slow.invalid", "https://good.invalid"]}
    }
    p = FakeProvider("p", config)
    deadlines = []

    def fetch(endpoint, query, context, deadline):
        deadlines.append(deadline)
        if "slow" in endpoint:
            time.sleep(max(0, deadline - time.monotonic()) + 0.005)
            raise ProviderError("deadline", retryable=True)
        return [Release("Example", source="p", infohash=HASH)]

    p.fetch = fetch
    end = time.monotonic() + 0.2
    found = p.search("Example", SearchContext("Example"), end, "id")
    assert found and deadlines[0] < deadlines[1] <= end
    assert p.mirrors["https://slow.invalid"].failure_count == 1
