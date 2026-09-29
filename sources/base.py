"""Shared bounded HTTP transport; credentials never enter diagnostics."""

import json
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .health import Health, ProviderError
from .models import Release
from .transport import fetch_response

LOG = logging.getLogger("popcorn.sources")
if not LOG.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(handler)
LOG.setLevel(os.environ.get("POPCORN_SOURCE_LOG_LEVEL", "INFO").upper())
LOG.propagate = False


def event(**fields):
    LOG.info(json.dumps(fields, ensure_ascii=False, sort_keys=True))


class Provider:
    id = name = ""
    priority = 50
    endpoints = ()
    json_response = True

    def __init__(self, config):
        self.config = config
        overrides = config.providers.get(self.id, {})
        self.enabled = overrides.get("enabled", True)
        self.priority = int(overrides.get("priority", self.priority))
        self.endpoints = tuple(overrides.get("endpoints", self.endpoints))
        if not self.endpoints or any(
            urlsplit(x).scheme not in {"http", "https"} for x in self.endpoints
        ):
            raise ValueError(f"Invalid endpoints for {self.id}")
        self.health = Health(config)
        self.mirrors = {endpoint: Health(config) for endpoint in self.endpoints}
        self.busy = threading.Lock()

    def diagnostics(self):
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            **self.health.snapshot(),
            # Even paths/hostnames can contain tokens: use stable pool IDs.
            "endpoints": [
                {"endpoint": f"mirror-{i + 1}", **self.mirrors[e].snapshot()}
                for i, e in enumerate(self.endpoints)
            ],
        }

    def healthcheck(self):
        """Passive runtime health; normal searches provide recovery probes."""
        return self.health.snapshot()

    def endpoint_order(self):
        def order(endpoint):
            state = self.mirrors[endpoint].snapshot()
            category = {"HEALTHY": 0, "UNKNOWN": 1, "DEGRADED": 2, "OPEN": 3}[
                state["state"]
            ]
            return (
                category,
                -(state["last_success"] or 0),
                state["latency_ms"] or float("inf"),
            )

        return sorted(self.endpoints, key=order)

    def request(
        self, url, deadline, *, params=None, headers=None, json_response=None, raw=False
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("deadline", retryable=True)
        expected_json = self.json_response if json_response is None else json_response
        try:
            with fetch_response(
                url,
                params=params,
                headers={
                    "User-Agent": "Popcorn/1.0",
                    "Accept": "application/json" if expected_json else "text/html",
                    **(headers or {}),
                },
                connect_timeout=min(self.config.connect_timeout, remaining / 2),
                read_timeout=min(self.config.read_timeout, remaining / 2),
                total_timeout=remaining,
                max_bytes=self.config.max_response_bytes,
                proxy=os.environ.get("POPCORN_SOURCE_PROXY"),
            ) as response:
                status = response.status_code
                if status >= 400:
                    after = response.headers.get("Retry-After", "0")
                    try:
                        retry_after = float(after)
                    except ValueError:
                        try:
                            retry_after = (
                                parsedate_to_datetime(after)
                                - datetime.now(timezone.utc)
                            ).total_seconds()
                        except (TypeError, ValueError):
                            retry_after = 0
                    raise ProviderError(
                        "http",
                        status=status,
                        retryable=status in {500, 502, 503, 504},
                        retry_after=min(86400, max(0, retry_after)),
                    )
                if (
                    expected_json
                    and "json" not in response.headers.get("Content-Type", "").lower()
                ):
                    raise ProviderError("content_type", status=status)
                chunks, length = [], 0
                for chunk in response.iter_content(16384):
                    length += len(chunk)
                    if time.monotonic() >= deadline:
                        raise ProviderError("deadline", retryable=True)
                    if length > self.config.max_response_bytes:
                        raise ProviderError("invalid_payload", status=status)
                    chunks.append(chunk)
                body = b"".join(chunks).decode(
                    response.encoding or "utf-8", errors="replace"
                )
                if not body.strip():
                    raise ProviderError("invalid_payload", status=status)
                if any(
                    marker in body[:20000].lower()
                    for marker in (
                        "just a moment",
                        "cf-chl-",
                        "verify you are human",
                        "captcha",
                        "access denied",
                    )
                ):
                    raise ProviderError("challenge", status=status)
                if expected_json:
                    try:
                        return json.loads(body)
                    except ValueError as exc:
                        raise ProviderError("invalid_json", status=status) from exc
                if raw:
                    if "xml" not in response.headers.get("Content-Type", "").lower():
                        raise ProviderError("content_type", status=status)
                    return body
                # A redirected homepage must never masquerade as search results.
                if urlsplit(url).path != "/" and urlsplit(response.url).path in {
                    "",
                    "/",
                }:
                    raise ProviderError("invalid_payload", status=status)
                return BeautifulSoup(body, "html.parser")
        except requests.Timeout as exc:
            raise ProviderError("timeout", retryable=True) from exc
        except requests.TooManyRedirects as exc:
            raise ProviderError("redirect_loop") from exc
        except requests.exceptions.SSLError as exc:
            raise ProviderError("tls") from exc
        except requests.ConnectionError as exc:
            raise ProviderError("network", retryable=True) from exc
        except requests.RequestException as exc:
            raise ProviderError("request") from exc

    def search(self, query, context, deadline, search_id):
        if not self.health.acquire():
            raise ProviderError("circuit_open")
        started = time.monotonic()
        error = ProviderError("no_endpoint")
        endpoints = [
            e
            for e in self.endpoint_order()
            if self.mirrors[e].snapshot()["state"] != "OPEN"
        ]
        for index, endpoint in enumerate(endpoints):
            mirror = self.mirrors[endpoint]
            if not mirror.acquire():
                continue
            endpoint_started = time.monotonic()
            # Reserve budget for an equivalent endpoint instead of letting
            # retries on one dead mirror consume the entire provider deadline.
            endpoint_deadline = deadline
            if index < len(endpoints) - 1:
                endpoint_deadline = (
                    endpoint_started + max(0, deadline - endpoint_started) / 2
                )
            for attempt in range(self.config.retries + 1):
                try:
                    results = self.fetch(endpoint, query, context, endpoint_deadline)
                    if time.monotonic() > endpoint_deadline:
                        raise ProviderError("deadline", retryable=True)
                    if not isinstance(results, list) or any(
                        not isinstance(r, Release) for r in results
                    ):
                        raise ProviderError("schema")
                    mirror.success(time.monotonic() - endpoint_started)
                    self.health.success(time.monotonic() - started)
                    event(
                        search_id=search_id,
                        query=query,
                        provider=self.id,
                        endpoint=f"mirror-{self.endpoints.index(endpoint) + 1}",
                        attempt=attempt + 1,
                        latency_ms=round((time.monotonic() - started) * 1000),
                        result_count=len(results),
                        cache_hit=False,
                        health_state="HEALTHY",
                        error_type=None,
                        status_code=200,
                    )
                    return results
                except Exception as exc:
                    error = (
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError("parser")
                    )
                    event(
                        search_id=search_id,
                        query=query,
                        provider=self.id,
                        endpoint=f"mirror-{self.endpoints.index(endpoint) + 1}",
                        attempt=attempt + 1,
                        latency_ms=round((time.monotonic() - started) * 1000),
                        result_count=0,
                        cache_hit=False,
                        health_state="DEGRADED",
                        error_type=error.reason,
                        status_code=error.status,
                        exception_type=type(exc).__name__,
                    )
                    if not error.retryable or attempt == self.config.retries:
                        break
                    delay = (
                        random.uniform(0.5, 1.5)
                        * self.config.retry_backoff
                        * 2**attempt
                    )
                    if time.monotonic() + delay >= endpoint_deadline:
                        break
                    time.sleep(delay)
            mirror.failure(error, time.monotonic() - endpoint_started)
            if time.monotonic() >= deadline:
                break
        self.health.failure(error, time.monotonic() - started)
        raise error

    def probe_endpoint(self, endpoint, context, deadline, search_id):
        mirror = self.mirrors[endpoint]
        if not mirror.acquire_probe():
            return
        started = time.monotonic()
        try:
            found = self.fetch(endpoint, context.queries(1)[0], context, deadline)
            if time.monotonic() > deadline or not isinstance(found, list):
                raise ProviderError("deadline")
            mirror.success(time.monotonic() - started)
            error = None
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else ProviderError("parser")
            mirror.failure(error, time.monotonic() - started)
            found = []
        event(
            search_id=search_id,
            query=context.title,
            provider=self.id,
            endpoint=f"mirror-{self.endpoints.index(endpoint) + 1}",
            attempt=1,
            latency_ms=round((time.monotonic() - started) * 1000),
            result_count=len(found),
            cache_hit=False,
            probe=True,
            health_state=mirror.snapshot()["state"],
            error_type=error.reason if error else None,
            status_code=error.status if error else 200,
        )

    def resolve(self, release):
        # Most indexes carry infohashes; downloader builds their magnet locally.
        if release.infohash or release.magnet:
            return release
        deadline = time.monotonic() + self.config.provider_timeout
        parsed = urlsplit(release.url)
        # Resolve only URLs emitted by configured endpoints, never arbitrary URLs.
        endpoint = next(
            (e for e in self.endpoints if parsed.netloc == urlsplit(e).netloc), None
        )
        if not endpoint:
            raise ProviderError("untrusted_detail_url")
        if not self.health.acquire():
            raise ProviderError("circuit_open")
        started = time.monotonic()
        mirror = self.mirrors[endpoint]
        if not mirror.acquire():
            error = ProviderError("circuit_open")
            self.health.failure(error, 0)
            raise error
        error = None
        try:
            soup = self.request(release.url, deadline, json_response=False)
            link = soup.select_one("a[href^='magnet:']")
            if not link:
                raise ProviderError("schema")
            resolved = Release(**{**release.__dict__, "magnet": link["href"]})
            if not resolved.infohash:
                raise ProviderError("invalid_payload")
            mirror.success(time.monotonic() - started)
            self.health.success(time.monotonic() - started)
            return resolved
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else ProviderError("parser")
            mirror.failure(error, time.monotonic() - started)
            self.health.failure(error, time.monotonic() - started)
            raise error from exc
        finally:
            event(
                search_id="resolve-" + uuid.uuid4().hex,
                query=release.title,
                provider=self.id,
                endpoint=f"mirror-{self.endpoints.index(endpoint) + 1}",
                attempt=1,
                latency_ms=round((time.monotonic() - started) * 1000),
                result_count=0 if error else 1,
                cache_hit=False,
                health_state=self.health.snapshot()["state"],
                error_type=error.reason if error else None,
                status_code=error.status if error else 200,
            )


def rows(payload, key=None):
    value = (
        payload.get(key)
        if key and isinstance(payload, dict)
        else payload
        if key is None
        else None
    )
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ProviderError("schema")
    return value


def release_row(title, infohash, provider, seeds=0, peers=0, size=0, **kwargs):
    if isinstance(size, str) and re.fullmatch(r"\d+(?:\.\d+)?", size.strip()):
        size = int(float(size))
    result = Release(
        title=str(title or ""),
        infohash=infohash,
        source=provider,
        seeds=seeds,
        peers=peers,
        size=str(size),
        size_bytes=size if isinstance(size, int) else 0,
        **kwargs,
    )
    if not result.title or not result.infohash:
        raise ProviderError("schema")
    if isinstance(size, (int, float)):
        result.size = f"{size / 1024**3:.2f} GiB"
    return result
