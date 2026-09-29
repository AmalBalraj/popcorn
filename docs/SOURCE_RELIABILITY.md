# Source discovery and durable download jobs

Use Popcorn only for content the operator is authorized to download. No adapter
solves challenges, bypasses authentication, or supplies third-party credentials
to the browser. Existing public endpoints are retained, not endorsed as stable.

## Existing architecture and the failure modes addressed

The browser posts a title to `/api/search`. Previously `web_app.py` called six
functions in `popcorn.py`, used a global search lock while changing the process
DNS resolver and timeout, and waited for every future. Some functions swallowed
request/parser failures into `[]`, so a broken schema looked like no matches.
Mirrors had no durable runtime health. Deduplication discarded metadata from
other sources and selection primarily followed seeds/quality. A selection was
kept only in memory, resolved via `magnet_for_result`, then downloaded by a web
thread, moved through rclone, and refreshed/polled in Jellyfin.

SQLite persisted job JSON, but startup deleted partial folders and interrupted
active downloads. Upload failures kept files without providing an independent
retry path. An indexing failure could still produce a completed job.

## Current flow

```
POST /api/search -> SearchContext -> bounded SearchService
                    | canonical/year/episode + compact aliases
                    | independent provider adapters + mirror health/breakers
                    | killable HTTP exchanges with an absolute deadline
                    v
                 canonical Release -> merge hashes -> weighted rank -> UI groups

POST /api/download -> SQLite selected release + owned working directory
                    -> resolve only if hash is absent
                    -> aria2 supervisor -> durable exit record + .aria2 pieces
                    -> download_complete checkpoint
                    -> rclone copy supervisor -> upload_complete checkpoint
                    -> remove local copy only after the checkpoint is durable
                    -> refresh mount/Jellyfin and find media path
                    -> complete
```

The normal web request queries all enabled providers concurrently. A saved
preferred source influences ordering; it does not remove fallback providers.
An explicit API `source` and CLI `--source` still select just that provider.
Source IDs and the frontend's search/result IDs and stage names remain intact.

The fixed pool has no unbounded work queue: requests consume a slot before
submission and each provider has one in-flight lease. When concurrency is
smaller than the provider count, the remaining providers get turns as slots
free. Searches never queue behind another user's outstanding provider request.
The default deadline is 7 seconds; each provider gets at most 6 seconds including
variants, endpoint failover, retries and jitter. Once useful results arrive,
the default grace period is 1.5 seconds to collect other answers. A hung source
does not set the response time. Pending sources may finish and populate their
provider cache after the response; result selections already shown stay stable.

Each HTTP exchange runs in a short-lived child process. Credentials travel over
stdin, not process arguments; HTTP bodies are capped at 4 MiB. The parent kills
and reaps a child exceeding the remaining deadline, including DNS stalls and
trickling bodies. This costs a Python/requests startup per exchange but prevents
a blocked resolver from occupying a worker forever.

## Adapters

| ID / module | Contract and resolution |
| --- | --- |
| `regional` / `sources/bitsearch.py` | BitSearch JSON API; optional server API key; infohash |
| `tpb` / `sources/apibay.py` | APiBay video JSON search; recognizes no-results sentinel; infohash |
| `yts` / `sources/yts.py` | YTS v2 movie JSON API; existing mirror pool; infohash |
| `torrents-csv` / `sources/torrents_csv.py` | JSON search; infohash |
| `rarbg` / `sources/rarbg.py` | Existing theRARBG HTML layout; movie/TV categories; lazy magnet detail |
| `tgx` / `sources/torrentgalaxy.py` | Existing TGx HTML layout and mirror pool; lazy magnet detail |
| `torznab` / `sources/torznab.py` | Optional configured Torznab RSS/XML API; infohash or magnet |

Torznab follows the [Torznab specification](https://torznab.github.io/spec-1.3-draft/torznab/Specification-v1.3.html)
and [Jackett's documented query interface](https://github.com/Jackett/Jackett#api-usage).
It can connect an operator's authorized index, including a compatible
Jackett/Prowlarr endpoint. It is absent unless configured. Results that offer
only a downloadable `.torrent` file, without a magnet/hash, are skipped.
Equivalent endpoints of one index belong in a mirror pool. Unrelated indexes
should be separate provider adapters, not misrepresented as mirrors.

`Provider.fetch` contains parsing and endpoint-specific authentication;
`Provider.search` shares transport/validation/retry policy. `resolve` only
fetches a detail page when a hash is absent, and checks that its host belongs to
the configured provider. `healthcheck` is a passive runtime snapshot; real
searches and background probes establish health. To replace an adapter, edit its
one module and registry import in `sources/__init__.py`.

## Failure policy, caches and ranking

Providers and endpoints have independent success/failure counts, consecutive
failures, latency, last success/failure, reason/status, malformed count and
rate-limit timestamps. States are UNKNOWN, HEALTHY, DEGRADED and OPEN. Three
consecutive failures open a circuit for 30 seconds. A single half-open probe is
allowed after cooldown; repeated failures double it up to 900 seconds. HTTP 429
uses `Retry-After` (seconds or HTTP date), bounded to one day. Transient network
errors, timeouts and selected 5xx can retry; permanent HTTP errors, TLS validation
errors, malformed JSON, schema changes and challenges cannot.

Healthy mirrors are preferred using recent success and latency. Failed mirrors
are deprioritized and open mirrors are skipped. Recovery probes for cooled
providers/mirrors use spare slots in the same bounded pool without delaying
cached responses. No background request is created per dead mirror per search.

200 responses must have the expected JSON/XML content type and schema. Challenge
pages, invalid JSON, blank bodies, oversized bodies, redirect loops, redirects
to a homepage, and unrecognized HTML layouts fail explicitly. A valid API empty
result is a success. Unexpected adapter exceptions are isolated and counted as
parser failures, not propagated to `/api/search`.

Query and provider caches are bounded, copied on read, and keyed by the full
context (title/year/type/season/episode/quality/language/codec/aliases) and source
selection. Successful results live for 120 seconds. Identical provider failures
are cached for 5 seconds to reduce repeated failures before the circuit opens.
If every live request fails or is unavailable, a prior query up to 30 minutes old
is returned with `stale=true`. Valid, fresh empty responses do not revive stale
matches. Health and caches are runtime state, not SQLite data, and reset on
service restart. Recovery probes also run on fresh query-cache hits.

TMDB canonical/original titles and transliteration aliases use existing
application metadata support. TMDB enrichment runs separately with at most two
in-flight lookups, caches aliases for 30 minutes, and never delays torrent
search. The first search may precede enrichment; subsequent searches use it.
At most three variants are searched per provider, stopping when results appear.

Release metadata is canonical at the adapter boundary. Hex/base32 v1 infohashes
normalize to lowercase hex. Hash duplicates merge sources, trackers, counts and
missing metadata. Without hashes, only matching normalized release names and
sizes merge; unknown-size rows from different providers remain separate.
`sources/models.py:WEIGHTS` centralizes scoring, and `score_details` exposes each
contribution. Title/year/type and season/episode matches dominate logarithmically
capped seeds; requested quality, codec/language, size sanity, source quality,
adapter confidence and corroboration contribute. Parsed filename metadata and
reported seed counts are evidence, not proof. Trackers belong to `downloader.py`,
are configured separately, and deduplicated into locally built magnets.

## Recovery and operations

All selected-release metadata and download/upload checkpoints persist in the
existing JSON job table without a schema migration. Checkpoints must commit
before taking the next destructive action. Progress-only writes remain best
effort. A per-job flock prevents overlapping recovery workers; the detached
`transfer_runner.py` holds a separate transfer lock and writes atomic exit
records. Recovery verifies runner identity through `/proc`, not just a reused
PID. A stopped runner or `.aria2` state cannot become a successful torrent exit.

If the web worker dies while aria2/rclone runs, the new worker reattaches to its
supervisor. A full systemd stop can terminate both; the same owned directory and
control files are resumed after restart. aria2 saves state every 10 seconds,
checks pieces on resume, does not overwrite/rename outputs, uses DHT/PEX and 80
peers, and disables LPD by default on servers. DHT state is isolated per web job.
The systemd unit allows 90 seconds for graceful shutdown. Keep the one-worker
gunicorn deployment; multiple independent web workers still have separate job
and search-selection memory despite the filesystem transfer locks.

A scheduler checks every five seconds and retries the failed stage after 30
seconds, doubling up to 30 minutes, without a finite retry limit. It runs up to
three job workers. Upload failures retain the full local copy; successful
copies are checkpointed before deleting it. A crash between copy completion and
the checkpoint uses the supervisor's success record. Retried copies otherwise
skip matching remote files. Indexing persists until Jellyfin exposes the item;
neither torrent nor upload repeats. Download pages show the retry message and
offer **Retry now**. Job ownership applies to the retry API.

Explicit Stop still removes incomplete downloads. Completed local copies are
kept when upload is stopped, although remote files already copied remain there.
Removing a history row does not delete retained data or job control files.
Monitor disk space and remove abandoned files deliberately after inspection.

Older active upload/index jobs can use their stage as a checkpoint. Older active
download jobs lacking the selected release are marked interrupted with files
retained, because their source cannot be reconstructed safely. Already terminal
legacy failures are not automatically restarted. Files removed by the old
startup logic cannot be recovered by this migration.

`GET /api/admin/providers` is admin-only and returns health plus opaque mirror
IDs. It never returns endpoint URLs, API keys, or proxy credentials. JSON source
events include search_id/query/provider/endpoint/attempt/latency_ms/result_count/
cache_hit/health_state/error_type/status_code. Endpoint IDs map to configured
pool order; inspect server configuration to identify the URL. Exceptions are
logged by type without credential-bearing exception strings.

## Configuration

No new settings are required. All values below are server environment variables;
seconds are numeric seconds. Provider overrides are JSON in one environment
variable, avoiding arbitrary new fields in the browser's settings API.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `POPCORN_SOURCE_CONNECT_TIMEOUT` | `2` | Connect timeout per HTTP exchange |
| `POPCORN_SOURCE_READ_TIMEOUT` | `3` | Read inactivity timeout |
| `POPCORN_SOURCE_PROVIDER_TIMEOUT` | `6` | Absolute budget per provider, including variants/retries |
| `POPCORN_SOURCE_SEARCH_DEADLINE` | `7` | Overall search deadline; web timeout can lower it |
| `POPCORN_SOURCE_EARLY_RETURN` | `1.5` | Collection grace after the first useful answer |
| `POPCORN_SOURCE_MIN_RESULTS` | `1` | Minimum raw results to enable early return |
| `POPCORN_SOURCE_CONCURRENCY` | `6` | Maximum simultaneous search/probe workers/HTTP exchanges |
| `POPCORN_SOURCE_RETRIES` | `1` | Extra attempts per eligible endpoint failure |
| `POPCORN_SOURCE_RETRY_BACKOFF` | `0.2` | Initial backoff, with 0.5–1.5 jitter and doubling |
| `POPCORN_SOURCE_FAILURE_THRESHOLD` | `3` | Consecutive failures to open circuit |
| `POPCORN_SOURCE_COOLDOWN` | `30` | Initial circuit cooldown and mirror recovery interval |
| `POPCORN_SOURCE_MAX_COOLDOWN` | `900` | Circuit backoff cap |
| `POPCORN_SOURCE_CACHE_TTL` | `120` | Fresh query/provider results TTL |
| `POPCORN_SOURCE_FAILURE_CACHE_TTL` | `5` | Identical provider failure TTL |
| `POPCORN_SOURCE_STALE_TTL` | `1800` | Maximum age of fallback query results |
| `POPCORN_SOURCE_CACHE_ENTRIES` | `256` | Maximum entries per cache |
| `POPCORN_SOURCE_VARIANTS` | `3` | Maximum query variants per provider |
| `POPCORN_SOURCE_MAX_RESPONSE_BYTES` | `4194304` | Maximum HTTP body size |
| `POPCORN_SOURCE_PROVIDERS` | `{}` | Per-ID overrides: enabled boolean, priority integer, endpoints URL list |
| `POPCORN_SOURCE_PROXY` | unset | Optional operator-configured HTTP/SOCKS egress proxy |
| `POPCORN_SOURCE_LOG_LEVEL` | `INFO` | Structured source event logging level |
| `POPCORN_TORZNAB_API_KEY` | unset | Optional Torznab server-side credential |
| `POPCORN_TRACKERS` | five existing public UDP trackers | Comma-separated trackers; empty disables defaults |
| `POPCORN_ARIA2_MAX_PEERS` | `80` | Peer cap |
| `POPCORN_ARIA2_LPD` | `false` | Local peer discovery on trusted LANs |
| `POPCORN_ARIA2_ALLOCATION` | `falloc` | File allocation; use `none` where falloc is unsupported |
| `POPCORN_JOB_RETRY_BASE` | `30` | Initial failed-stage retry delay |
| `POPCORN_JOB_RETRY_MAX` | `1800` | Retry delay cap |
| `POPCORN_JOB_MAX_WORKERS` | `3` | Active download/upload/index worker limit |
| `POPCORN_JOB_RECOVERY` | `1` | Enable automatic startup/retry scheduler; `0` for tests/maintenance |
| `POPCORN_STATE_FILE` | `data/popcorn.sqlite3` in project | Job database path; primarily useful for isolated tests |

Existing `POPCORN_BITSEARCH_API_KEY`, rclone destination, Jellyfin server key,
TMDB key and login settings retain their meaning. Existing `private_dns` is
retained in saved settings for compatibility, but web searches no longer patch
process-wide DNS and its switch was removed. Configure server DNS/egress at the
host; the legacy CLI `--doh` remains explicit. No challenge bypass was added.

Example systemd EnvironmentFile values (no inline comments on value lines):

```ini
POPCORN_SOURCE_PROVIDERS={"regional":{"priority":10},"tgx":{"enabled":true},"torznab":{"enabled":true,"priority":5,"endpoints":["http://127.0.0.1:9117/api/v2.0/indexers/your-index/results/torznab/api"]}}
POPCORN_TORZNAB_API_KEY=your_server_side_key
POPCORN_SOURCE_CONCURRENCY=6
```

Use a different adapter ID for each independent configured index when extending
the registry; the example pool is one index. Default priorities are regional 10,
torrents-csv 20, tpb 30, yts 40, rarbg 50, tgx 60; optional Torznab is 5.
Endpoint defaults are in the individual modules. Restart the service to reload
environment values; do not delete job working directories or `.aria2` files.

The CLI also avoids an unrelated internet-check prerequisite: a blocked
connectivity-check host cannot prevent otherwise reachable providers from
answering. Its legacy explicit DNS option runs inside isolated HTTP children.
Transfer logs retain numeric progress only, discarding credential-bearing
command output; failures report their exit status separately.

## Verification and known limits

Run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`. The suite covers the
requested A–H failure scenarios, schemas/content types, caching, concurrency,
breaker/mirror recovery, credentials/authorization, SQLite migration/checkpoint
failure, durable subprocess reattachment, and actual aria2 interruption/resume
against a localhost webseed containing generated fixture data. Socket tests need
localhost network permission. The aria2 integration skips if aria2c is absent.

No third-party endpoint is certified healthy by these tests. Public APIs can
change or disappear; theRARBG/TGx HTML remains brittle. Torznab reliability also
depends on the configured service/index. Runtime caches and health are lost on
restart. Early return can omit late releases until a later uncached search.
This is one server's SQLite/thread architecture, not a distributed durable queue.
Disk exhaustion, deleted completed files, revoked credentials, lack of peers,
and torrents without indexable video can require operator intervention; retries
retain data but cannot repair those conditions. rclone copy completion is its
success contract; there is no independent remote checksum audit for remotes that
do not support it. CLI downloads retain their simpler foreground lifecycle;
durable stage recovery applies to web jobs.
