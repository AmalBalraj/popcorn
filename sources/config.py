"""Operational source settings; provider overrides are server-side JSON."""

import json
import math
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    connect_timeout: float = 2
    read_timeout: float = 3
    provider_timeout: float = 6
    search_deadline: float = 7
    early_return: float = 1.5
    min_results: int = 1
    concurrency: int = 6
    retries: int = 1
    retry_backoff: float = 0.2
    failure_threshold: int = 3
    cooldown: float = 30
    max_cooldown: float = 900
    cache_ttl: float = 120
    failure_cache_ttl: float = 5
    stale_ttl: float = 1800
    cache_entries: int = 256
    variants: int = 3
    max_response_bytes: int = 4 * 1024 * 1024
    providers: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls):
        values = {}
        for name, definition in cls.__dataclass_fields__.items():
            raw = os.environ.get(f"POPCORN_SOURCE_{name.upper()}")
            if raw is not None:
                values[name] = (
                    json.loads(raw) if name == "providers" else definition.type(raw)
                )
        config = cls(**values)
        for name in values:
            if (
                name != "providers"
                and (
                    not math.isfinite(getattr(config, name))
                    or getattr(config, name) <= 0
                )
                and name != "retries"
            ):
                raise ValueError(f"POPCORN_SOURCE_{name.upper()} must be positive")
        if config.retries < 0 or config.stale_ttl < config.cache_ttl:
            raise ValueError("Invalid source retry/cache settings")
        if not isinstance(config.providers, dict):
            raise ValueError("POPCORN_SOURCE_PROVIDERS must be a JSON object")
        for override in config.providers.values():
            if not isinstance(override, dict) or not isinstance(
                override.get("enabled", True), bool
            ):
                raise ValueError("Provider overrides require a boolean enabled value")
            if "endpoints" in override and (
                not isinstance(override["endpoints"], list)
                or not all(isinstance(e, str) for e in override["endpoints"])
            ):
                raise ValueError("Provider endpoints must be a URL list")
        return config
