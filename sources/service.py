"""Bounded aggregation without waiting for stragglers or queuing unbounded work."""

import copy
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass

from .base import event
from .health import ProviderError
from .models import merge_releases, rank


@dataclass
class SearchResult:
    search_id: str
    releases: list
    providers: dict
    stale: bool = False
    cache_hit: bool = False


class SearchService:
    def __init__(self, providers, config):
        self.providers = {p.id: p for p in providers}
        self.config = config
        self.executor = ThreadPoolExecutor(
            max_workers=config.concurrency, thread_name_prefix="source"
        )
        self.slots = threading.BoundedSemaphore(config.concurrency)
        self.lock = threading.Lock()
        self.cache, self.provider_cache, self.failure_cache = {}, {}, {}

    def _put(self, cache, key, value):
        with self.lock:
            if len(cache) >= self.config.cache_entries and key not in cache:
                cache.pop(next(iter(cache)))
            cache[key] = (time.monotonic(), copy.deepcopy(value))

    def _run(self, provider, context, deadline, search_id, refresh=False):
        start = time.monotonic()
        try:
            key = (provider.id, repr(asdict(context)))
            with self.lock:
                cached = self.provider_cache.get(key)
                failed = self.failure_cache.get(key)
            if (
                failed
                and not refresh
                and start - failed[0] < self.config.failure_cache_ttl
            ):
                return [], {
                    "count": 0,
                    "cache_hit": True,
                    "available": False,
                    "error": failed[1],
                }
            if cached and not refresh and start - cached[0] < self.config.cache_ttl:
                event(
                    search_id=search_id,
                    query=context.title,
                    provider=provider.id,
                    endpoint=None,
                    attempt=0,
                    latency_ms=0,
                    result_count=len(cached[1]),
                    cache_hit=True,
                    health_state=provider.health.snapshot()["state"],
                    error_type=None,
                    status_code=None,
                )
                return copy.deepcopy(cached[1]), {
                    "count": len(cached[1]),
                    "cache_hit": True,
                    "available": True,
                }
            found = []
            for query in context.queries(self.config.variants):
                found.extend(provider.search(query, context, deadline, search_id))
                if found or time.monotonic() >= deadline:
                    break
            self._put(self.provider_cache, key, found)
            with self.lock:
                self.failure_cache.pop(key, None)
            return found, {
                "count": len(found),
                "available": True,
                "elapsed": round(time.monotonic() - start, 2),
                "cache_hit": False,
            }
        except Exception as exc:
            # Includes adapter bugs outside the adapter's own exception handling.
            if not isinstance(exc, ProviderError):
                provider.health.failure(
                    ProviderError("parser"), time.monotonic() - start
                )
            self._put(
                self.failure_cache, key, getattr(exc, "reason", type(exc).__name__)
            )
            return [], {
                "count": 0,
                "available": False,
                "error": getattr(exc, "reason", type(exc).__name__),
            }
        finally:
            provider.busy.release()
            self.slots.release()

    def _schedule_probes(self, context, names, search_id):
        """Recovery probes use the same bounded pool and never block a response."""
        for name in names:
            provider = self.providers[name]
            if not provider.enabled:
                continue
            health = provider.health.snapshot()
            recovering = (
                health["open_until"]
                and health["open_until"] <= time.time()
                and health["rate_limit_until"] <= time.time()
            )
            endpoints = [
                e
                for e, h in provider.mirrors.items()
                if h.failure_count
                and max(
                    h.open_until,
                    h.rate_limit_until,
                    (h.last_failure or 0) + self.config.cooldown,
                )
                <= time.time()
            ]
            if not recovering and not endpoints:
                continue
            if not provider.busy.acquire(blocking=False):
                continue
            if not self.slots.acquire(blocking=False):
                provider.busy.release()
                break
            deadline = time.monotonic() + self.config.provider_timeout
            if recovering:
                self.executor.submit(
                    self._run, provider, context, deadline, search_id, True
                )
            else:

                def probe(p=provider, e=endpoints[0], end=deadline):
                    try:
                        p.probe_endpoint(e, context, end, search_id)
                    finally:
                        p.busy.release()
                        self.slots.release()

                self.executor.submit(probe)

    def search(
        self,
        context,
        names=None,
        *,
        deadline_seconds=None,
        refresh=False,
        preferred=None,
    ):
        started = time.monotonic()
        deadline = started + min(
            deadline_seconds or self.config.search_deadline, self.config.search_deadline
        )
        names = sorted(names or self.providers)
        search_id = uuid.uuid4().hex
        key = (repr(asdict(context)), tuple(names))
        with self.lock:
            cached = copy.deepcopy(self.cache.get(key))
        if cached and not refresh and started - cached[0] < self.config.cache_ttl:
            self._schedule_probes(context, names, search_id)
            event(
                search_id=search_id,
                query=context.title,
                provider="aggregate",
                endpoint=None,
                attempt=0,
                latency_ms=0,
                result_count=len(cached[1]),
                cache_hit=True,
                health_state=None,
                error_type=None,
                status_code=None,
            )
            return SearchResult(search_id, cached[1], {}, cache_hit=True)
        pending, statuses, results = {}, {}, []
        waiting = sorted(
            (self.providers[n] for n in names),
            key=lambda p: (
                p.health.snapshot()["state"] not in {"HEALTHY", "UNKNOWN"},
                p.id != preferred,
                p.priority,
            ),
        )

        def dispatch():
            while waiting:
                provider = waiting[0]
                if not provider.enabled:
                    statuses[provider.id] = {"available": False, "error": "disabled"}
                    waiting.pop(0)
                    continue
                if provider.health.snapshot()["state"] == "OPEN":
                    statuses[provider.id] = {
                        "available": False,
                        "error": "circuit_open",
                    }
                    waiting.pop(0)
                    continue
                if not provider.busy.acquire(blocking=False):
                    statuses[provider.id] = {"available": False, "error": "in_flight"}
                    waiting.pop(0)
                    continue
                if not self.slots.acquire(blocking=False):
                    provider.busy.release()
                    break
                waiting.pop(0)
                try:
                    future = self.executor.submit(
                        self._run,
                        provider,
                        context,
                        min(deadline, time.monotonic() + self.config.provider_timeout),
                        search_id,
                        refresh,
                    )
                except Exception:
                    provider.busy.release()
                    self.slots.release()
                    raise
                pending[future] = provider.id

        dispatch()
        first_result = None
        while (pending or waiting) and time.monotonic() < deadline:
            dispatch()
            if not pending:
                # Other searches own the slots. Never queue behind them.
                break
            stop = deadline
            if first_result is not None and len(results) >= self.config.min_results:
                stop = min(stop, first_result + self.config.early_return)
            ready, _ = wait(
                pending,
                timeout=max(0, stop - time.monotonic()),
                return_when=FIRST_COMPLETED,
            )
            if not ready:
                break
            for future in ready:
                name = pending.pop(future)
                found, statuses[name] = future.result()
                results.extend(found)
                if found and first_result is None:
                    first_result = time.monotonic()
        for name in pending.values():
            statuses[name] = {"available": False, "error": "pending", "count": 0}
        for provider in waiting:
            statuses[provider.id] = {
                "available": False,
                "error": "capacity",
                "count": 0,
            }
        # No executor context manager: it would wait for unfinished requests here.
        results = merge_releases(copy.deepcopy(results))
        results.sort(key=lambda r: rank(r, context), reverse=True)
        stale = False
        if results:
            self._put(self.cache, key, results)
        elif (
            cached
            and started - cached[0] < self.config.stale_ttl
            and not any(s.get("available") for s in statuses.values())
        ):
            results, stale = cached[1], True
        event(
            search_id=search_id,
            query=context.title,
            provider="aggregate",
            endpoint=None,
            attempt=0,
            latency_ms=round((time.monotonic() - started) * 1000),
            result_count=len(results),
            cache_hit=stale,
            stale=stale,
            health_state=None,
            error_type=None,
            status_code=None,
        )
        self._schedule_probes(context, names, search_id)
        return SearchResult(search_id, results, statuses, stale=stale)

    def resolve(self, release):
        if release.infohash:
            return release
        provider = self.providers.get(release.source)
        if not provider:
            raise ProviderError("unknown_provider")
        if not provider.busy.acquire(blocking=False):
            raise ProviderError("in_flight")
        try:
            return provider.resolve(release)
        finally:
            provider.busy.release()

    def diagnostics(self):
        return [p.diagnostics() for p in self.providers.values()]
