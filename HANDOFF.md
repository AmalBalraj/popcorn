# HANDOFF — popcorn

## Goal

CLI tool to search torrent sites for movies, display results with seeders/peers,
let the user pick one, and download via magnet link + aria2c.

## Current state

### Reliability architecture update (2026-09-28)

The implementation and operational defaults are documented in
[docs/SOURCE_RELIABILITY.md](docs/SOURCE_RELIABILITY.md). This section supersedes
the historical endpoint availability and timeout notes below; those are dated
observations, not ongoing guarantees.

- `sources/` owns independent adapters, canonical releases, normalization,
  scoring, bounded concurrent aggregation, mirror health, circuits and caches.
  All six original providers remain; configured Torznab is optional.
- Web searches no longer hold `SEARCH_LOCK` or alter global DNS/timeouts.
  Preferred-source settings keep fallbacks; explicit API/CLI source selection
  still limits the search. HTTP subprocess deadlines include DNS/body stalls.
- `downloader.py` owns configurable, deduplicated trackers and local magnets.
- Web jobs persist selected releases plus download/upload checkpoints in the
  existing SQLite payload. `transfer_runner.py` and `job_transfer.py` supervise
  transfers, preserve `.aria2` state, and reattach after web-worker crashes.
  rclone copies before checkpointed cleanup; upload/index failures retry their
  own stage. Older downloads without recovery metadata keep files and report
  the migration limit. Keep the deployment's single gunicorn worker.
- `GET /api/admin/providers` exposes runtime diagnostics without endpoint
  credentials; source events are structured JSON. The download UI exposes
  pending retry messages and an owner-protected Retry now action.
- The systemd unit allows 90 seconds for graceful transfer shutdown.
- Run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`. Tests exercise
  failures A–H, real localhost HTTP deadlines, detached subprocess recovery,
  and actual aria2 resume against generated authorized fixture data.

### Deployment verification (2026-09-28)

- Deployed in place and restarted `popcorn.service` at 07:05 UTC. Nginx's
  existing `popcorn.devmindset.in` TLS proxy to localhost:5050 was validated
  and left unchanged. HTTPS `/login` returned 200.
- All 58 automated tests passed immediately before deployment. Read-only
  live API checks confirmed anonymous diagnostics access returns 401,
  non-admin access returns 403, and signed admin access returns 200.
- A live all-source search returned 81 releases in 1.33 seconds; its repeat
  hit the query cache. BitSearch, APiBay, YTS, TorrentGalaxy and Torrents-CSV
  succeeded. theRARBG returned a schema-validation failure that was isolated
  from the other results. Availability is a point-in-time observation.
- No active transfers remained at restart. Existing job history was retained;
  no new downloads, remote uploads or library changes were initiated by smoke
  checks. A consistent SQLite and installed-unit backup is under
  `backups/deployment-20260928T070423Z/` (ignored by Git).

### Historical verification notes

### What works (download pipeline is fully functional)

- `python3 popcorn.py "Inception"` returns 100 results from The Pirate Bay and
  downloads work end-to-end on the Oracle server (verified: aria2c connects to
  peers via trackers and fetches torrent data).
- **Primary web source: regional** — BitSearch's JSON API. It embeds
  info-hashes, has broad Indian-language coverage, and is enriched with TMDB
  title/year aliases plus common transliteration variants. Responses are cached
  for ten minutes. Anonymous access works; `POPCORN_BITSEARCH_API_KEY` raises
  the upstream daily quota when configured.
- Jellyfin sessions use a unique device ID per Popcorn login. Background
  library refreshes use `POPCORN_JELLYFIN_API_KEY`; provision it with
  `sudo ./deploy/provision-jellyfin-api-key.sh`. Jellyfin 12 API-key requests
  must omit `Device` and `DeviceId` from the authorization header.
- **Fallback source: tpb** — The Pirate Bay JSON API (`apibay.org`). It embeds
  the infohash in every search result, so the magnet link is built directly
  with no second request. This is what fixed the old magnet-fetch blocker.
- Magnet links include 5 public UDP trackers so aria2c finds peers without
  relying solely on DHT.
- `aria2c` is installed (1.36.0).
- Six sources, all verified working from the Oracle box (2026-09-16):
  regional/BitSearch (JSON API, default), tpb (JSON API), rarbg (HTML scrape —
  theRARBG, movie + TV), YTS (JSON API), TorrentGalaxy (HTML scrape), and
  torrents-csv (JSON API — DHT-scraped, covers regional titles). All six build
  magnets directly or fetch them from a /post-detail page; none need a proxy,
  an API key or `--doh`.
- Speed tuning in aria2 (`ARIA2_FLAGS`): unlimited peers, aggressive
  peer-pulling, DHT/PEX/LPD on, fixed listen ports 6881-6889.
- `--upload-to REMOTE` (e.g. `gdrive:Movies`): downloads into a temp dir, then
  `rclone move`s it to the remote and deletes the local copy. Requires rclone
  configured. Without it, downloads land in the current directory.
  **Verified end-to-end** on the Oracle server: download → upload to Google
  Drive → local temp dir cleaned up automatically.
- rclone is installed and configured with a `gdrive` remote (full `drive`
  scope, headless OAuth token). `rclone about gdrive:` and a download→upload
  round-trip both confirmed working.
- BT listen ports **6881-6889 are open inbound** (ufw + Oracle Cloud security
  list), so aria2 accepts incoming peer connections — better swarm reach.
  BitTorrent speed is still ultimately capped by the swarm's seeder count.
- Mirror rotation — each source tries up to 5 domains until one responds.
- `--doh` flag bypasses Oracle's DNS blocking by resolving hostnames through
  Cloudflare DNS-over-HTTPS (`https://cloudflare-dns.com/dns-query`).
- `--proxy` flag for routing through SOCKS/HTTP proxy (requires
  `pip install 'requests[socks]'`).
- `--timeout` flag for configurable request timeouts (default 15s).
- `--source` lets you pick a single tracker (default: `tpb`).
- Connectivity check at startup — blocks early if no internet; warns instead
  of blocking when `--proxy` is set.

### Files

```
popcorn/
├── popcorn.py          # main script (~810 lines)
├── requirements.txt    # requests[socks], beautifulsoup4
└── CLAUDE.md           # untouched
```

## Regional coverage (replaced TamilBlasters, 2026-09-16)

The Western sources carry regional Indian titles only sparsely. TamilBlasters
used to fill that gap, but it can no longer be used:

- The IPS forum is **down at the origin** — `.garden` and `.gripe` both return
  Cloudflare **522** (confirmed from an independent network, so it is not an
  IP block on our side). The `.bet` entry domain still 301s to `.garden`.
- The domains that *do* answer are hijacked: `tamilmv.ws` now serves an
  Indonesian gambling site, `1tamilblasters.pro`/`.top` are ad-redirect clones
  rather than the forum.

Replaced by **Torrents-CSV** (`--source torrents-csv`), a DHT-scraped JSON
index. Unlike the forum it reports real seeder/peer counts, needs no `--doh`,
and still covers the titles TamilBlasters was added for. Verified: "Leo 2023"
25 results, "Manjummel Boys" 15, "Kumbalangi Nights" 3, all with live seeders.

```bash
python3 popcorn.py "Manjummel Boys" --source torrents-csv --search-only
python3 popcorn.py "Aavesham"       --source torrents-csv
```

## Resolved

### 1. Magnet-link fetch (was critical) — FIXED

Solved by switching the default source to The Pirate Bay's JSON API
(`apibay.org`), which embeds the infohash in every search result. The magnet
link is now built directly from the search response — there is no second
request to a detail page, so the Oracle connection-reset problem no longer
applies. Verified end-to-end: aria2c reaches peers and downloads.

## Resolved

### 2. TorrentGalaxy fails on Oracle — FIXED

`torrentgalaxy.to` no longer resolves and `.mx` is usually a 522 origin error,
so `MIRRORS_TG` now tries the working `.one` mirror first (both remain as
fallbacks). Verified: 174 results with no wasted 15s timeout on the dead hosts.

### 3. YTS fails everywhere — FIXED

Resolves and answers fine via `movies-api.accel.li`. Verified: 3 results for a
movie query.

### 4. 1337x blocked by Cloudflare — REPLACED

Every mirror now answers with a Cloudflare "Just a moment..." **403** (or has
stopped resolving). The domains are correct; the server's datacenter IP is
being challenged, and `cloudscraper` cannot run on this box anyway (it fails on
an `urllib3`/`requests_toolbelt` import clash). Replaced by **theRARBG**
(`--source rarbg`), the maintained RARBG successor: plain HTTP, no challenge,
no API key, and it carries the same release groups. Verified: 39 results for
"Inception 2010" and 60 for "Breaking Bad" (movie + TV categories are merged),
with working magnet extraction from `/post-detail/` pages.

Remove `POPCORN_1337X_PROXY` from `/etc/popcorn.env` if it is still set — no
code reads it any more.

### 5. "Still playing on Web" blocked deleting a file nobody was watching — FIXED

Symptom: deleting a title returned *"Still playing on Web. Stop playback there,
then delete."* while nothing was playing, and it came back after closing and
reopening the tab.

Cause: the player announced playback to Jellyfin but its **stop** report never
landed. `reportStopped` used `fetch(..., keepalive: true)` inside
`pagehide`/`beforeunload`, which browsers usually drop at unload, so Jellyfin
kept a stale `NowPlayingItem` and `_sessions_playing` blocked deletion for the
whole `PLAYBACK_GRACE_SECONDS` (180s) window. Reopening the player re-sent
`started` and restarted the window, which is what made it look permanent.
Evidence: every `Jellyfin Web` session that had played showed a cleared
`NowPlayingItem`, while the `Popcorn` session still held one ~7 minutes after
its last check-in.

Two fixes:

- **Client (`static/js/pages/player.js`)** — `reportStopped` now uses
  `navigator.sendBeacon()` (the API browsers guarantee to deliver during
  unload), falling back to the keepalive fetch. It also reports on
  `visibilitychange → hidden` **but only when the video is paused**, so
  background audio in another tab is not cut off.
- **Guard (`_sessions_playing`, `web_app.py`)** — dropped the
  `LastActivityDate` fallback. That field moves when the member merely browses
  the site — it was observed ticking to the second with no playback at all — so
  a stale `NowPlayingItem` plus ordinary browsing looked live forever. A
  session with no `LastPlaybackCheckIn` no longer blocks either.

Verified against live sessions: genuinely-playing and recently-paused sessions
still block the delete; stale ghosts, missing-timestamp sessions and
browse-only activity no longer do.

### 6. A downloaded "episode" was a Windows executable — FIXED

`Ted Lasso S04E07 1080p HEVC x265-MeGust.exe` (1.0 GB,
`application/x-ms-dos-executable`) downloaded "successfully" and uploaded to
`gdrive:Movies`, but never appeared in Jellyfin — it only indexes real media
containers, so a `.exe` is invisible. The job also never reached **Ready to
watch**; it stopped at "Jellyfin is still updating".

The name is bait: `-MeGust.exe` is built to read as the real release group
**`MeGusta`**. This is a malware lure, not a mislabelled video. Deleted from
`gdrive:Movies` and the whole library re-scanned — it was the only executable
among 75 files.

`_NOT_VIDEO` now rejects executable extensions (`.exe .scr .msi .msp .bat .cmd
.pif .vbs .vbe .js .jse .wsf .wsh .ps1 .jar .apk .ipa .dmg .pkg .lnk .reg .hta
.cpl .dll .deb .rpm`). The old filter only caught software keywords and files
under `MIN_FILM_BYTES` (80 MB), so a 1 GB `.exe` sailed through. `.com` and
`.app` are deliberately **not** blocked — they are real TLDs and release names
legitimately carry prefixes like `www.example.com - Title`.

Verified: 13 real titles from this library still pass, 8 lures are rejected.

### 7. Searches taking 20-30s — FIXED (worst case)

Healthy `--source all` searches were already fast: **1.3-1.9s** across six
sources. The slow searches had one cause — **apibay.org (TPB) hanging**, on top
of `_APIBAY_ATTEMPTS = 2` retrying at the *full* search timeout. Measured
before: min 1.64s, **median 20.3s, max 30.3s** (30s = exactly 2 x 15s), with
every slow query reporting `tpb` as the slowest source — while the other five
had answered inside 2s. Sources run in parallel, so the slowest one sets the
total.

`_query_apibay_mirror` now caps each attempt at `_APIBAY_TIMEOUT = 6s`, so the
worst case is 2 x 6s = **12s** instead of 30s. A healthy apibay answers in
~0.4s, so the normal path is untouched. Measured after: min 1.33s, median
1.63s, max 1.87s.

Still open, deliberately not changed:

- **`SEARCH_LOCK` serializes every search process-wide.** It exists because
  `popcorn._TIMEOUT` and the DoH `socket.getaddrinfo` patch are module-global,
  so the lock must be held for a whole search. Harmless for one viewer; with
  several members searching at once, one slow search delays the others.
  Fixing it means making that network config **thread-local** and dropping the
  lock — worth doing only if concurrent members become normal.
- **No overall deadline.** `all` waits for the slowest source, so any single
  hung upstream still stalls the whole search up to its timeout. Progressive
  results (return what is ready, let stragglers fill in) needs the same
  thread-local change to be safe.

## How to run

```bash
# Install deps (and aria2c, already installed on the Oracle server)
pip install -r requirements.txt
sudo apt install aria2

# Default: searches The Pirate Bay, downloads into the current dir
python3 popcorn.py "Inception"

# Download then auto-upload to Google Drive and free local disk
python3 popcorn.py "Inception" --upload-to gdrive:Movies

# Mainstream movie / TV releases:
python3 popcorn.py "Inception" --source rarbg

# Malayalam / South-Indian films:
python3 popcorn.py "Aavesham" --source torrents-csv
```

## Next step

All six sources are verified working from the Oracle box — no source needs
`--doh`, `--proxy` or an API key any more. Optional future work: `search_rarbg`
issues two requests per search (Movies + TV); if that shows up in latency, try
theRARBG's uncategorised listing and filter out `Books`/`Games`/`Music`/`XXX`
rows client-side instead.


## TV library and previews deployed — 2026-09-29

The live library now has 22 movies and three series with 26 episode files:
India's Got Latent (12 regular episodes + 10 specials), Ted Lasso (S04E06–07),
and Lanterns (S01E03–04). The former Students of Igloi movie was an incorrect
match for the entire 18.96 GiB IGL collection. No episode video was deleted or
redownloaded during migration; remote file sizes were verified after server-side moves.

`media_library.py` assigns verified TMDB show identities, parses season/episode
numbers, matches specials by provider title, and stages canonical source-independent
show/season folders with NFO metadata, artwork and matching subtitle sidecars.
Finished ambiguous downloads remain local in `needs_identification`; Downloads
offers show selection and per-file episode mapping before retry. Separate sources
share the same show identity and preserve distinct release files. The release picker
always shows the collection size. Download history now opens repaired series.

The read-only `popcorn-shows.service` mount exposes `gdrive:TV Shows` at
`/home/amal/gdrive-shows`, with rclone RC port 5573 and a separate VFS cache.
Jellyfin's TV library uses local metadata/artwork because its direct metadata route
was unreliable; scheduled trickplay extraction remains enabled. Import metadata
uses a cached, TLS-verified TMDB connection with DNS fallback.

Popcorn's player renders authenticated sprite previews for the selected media
source. Existing TV sheets were preserved across new item IDs; three previously
missing sources were generated locally. Retain `data/tv-migration` and
`data/preview-cache`: they supply those fallback images. Both are ignored by Git.
Episode stills now request Primary images through the correct route; inherited
backdrops use Backdrop. Series open the next-up or first regular season by default.

Migration tooling: `deploy/tv-library.py`; durable move manifest and preservation
assets under `data/tv-migration`. Database/configuration backups are recorded in
the manifest under `backups/tv-library-*`. Leftover Lanterns metadata was archived
on Drive under `Popcorn Migration Backups`; old TV movie records were removed.
Do not rerun `prepare` with a fresh post-migration inventory.

Validation: 76 Python tests, five JavaScript checks, all 26 remote sizes/episode
paths and artwork, and authenticated first/last thumbnail sheets for all 48
playback versions. Browser checks passed TV pages, episode stills, one-submit
search and real seek-preview image rendering (video duration was simulated to
avoid downloading a movie for the UI test). Public HTTPS login returned 200;
Popcorn and the new mount are active and enabled.


## Automatic subtitles deployed — 2026-09-29

`subtitle_downloads.py` implements the official OpenSubtitles.com REST API.
New downloads look for missing subtitles in the preferred playback language
before uploading; existing embedded/full sidecar subtitles are retained.
Matching uses a file hash first, then identity, episode coordinates and a strong
release-name match. Invalid subtitle responses, uncertain matches and provider
quota/outage failures do not stop the video upload.

Administrators connect their own username, password and API key at
**Settings → Subtitle downloads**. Credentials are verified before saving,
written atomically with mode 0600 under ignored `data/subtitles`, and excluded
from public settings, API responses and error text. No account is configured yet:
automatic fetching is enabled in preferences but awaits the owner's connection.
The live provider login/download must be verified after credentials are entered.

Connecting starts a background check of the existing library, including IGL.
The same section provides a manual check button for retries after quota resets.
Existing-library checks upload only matching `.eng.srt` (or the preferred-language
equivalent) files to the video's folder through rclone with `--immutable`, then
refresh the mounts/library. They do not change the read-only playback mounts.
No paid AI transcription or translation is called.

The new tests cover hash calculation, wrong episode/language/cut rejection,
preferred-language detection, subtitle preservation, quota failure, unsafe provider
responses, admin authorization, credential storage/redaction, download staging and
existing-library subtitle-only uploads. Browser checks passed the live disconnected
state and connection-form interaction with mocked account responses; those dummy
credentials never reached a provider. Existing artwork/preview routes still pass
for all 48 playback versions. User requested pushing all completed changes.
