# Popcorn

Popcorn is one private movie site for discovering releases, downloading them,
and watching the finished library. Downloads use `aria2c`; playback is powered
by Jellyfin behind the same Popcorn login and visual shell.

## Web interface

```bash
python3 -m pip install -r requirements.txt
python3 web_app.py
```

Open <http://127.0.0.1:5000>. The interface is organised around watching:

| Page | What it is for |
| --- | --- |
| **Home** | Continue watching, recently added, and genre shelves, under a featured hero |
| **Movies** / **TV Shows** | The whole library, filterable by genre and sortable |
| **Search** | One query, answered twice: what you already own, then what you could add |
| **Downloads** | Active transfers in human stages, plus everything fetched before |
| **Settings** | Playback, downloads, your account, and administration |

Opening a film or show gives a full detail page — artwork, cast, technical
information, similar titles — and **Play** goes straight to Popcorn's own
player, which resumes where you left off and reports progress back to Jellyfin
so Continue Watching stays correct on every device you watch on.

Library browsing, search, metadata and streaming are all served through
Popcorn: the browser never holds a Jellyfin credential, and artwork and video
are proxied with authentication added server-side.

The **Jellyfin** interface is still available in full from the profile menu
(and at `/watch`) for anything Popcorn does not cover.

### Adding a title

Search for a title, choose it, pick a quality, and press **Add**. Popcorn
groups the underlying releases by title, so you choose between *1080p BluRay*
and *720p WEB-DL* rather than between scene names; seeders, codecs and release
names live behind **Advanced release details**. Files are saved in this project
directory by default, or moved to the rclone destination configured in
**Settings → Downloads** (for example `gdrive:Movies`).

Web searches aggregate all enabled sources concurrently, with a saved preferred
source affecting ordering. A slow or broken source cannot fail the search.
Results appear after useful answers arrive, within a configurable deadline;
recent cached results provide a fallback during outages. Repeated failures open
temporary circuits, and recovery probes restore providers and mirrors.

**Indian & regional** uses BitSearch's JSON API and remains available. Spelling
variants and cached TMDB canonical/original titles improve title matching.
An optional `POPCORN_BITSEARCH_API_KEY` remains supported server-side.

The other sources are **RARBG** (mainstream movie and TV releases, including
2160p), **The Pirate Bay**, **YTS**, **TorrentGalaxy**, and **Torrents-CSV**
(a DHT-scraped JSON index). These six existing adapters remain enabled by
default; their current availability is reported at the admin-only
`GET /api/admin/providers`. An optional **Torznab** adapter supports an
operator-configured authorized index API. 1337x and TamilBlasters were dropped: 1337x
answers every mirror with a Cloudflare challenge from a datacenter IP, and
TamilBlasters' forum went down at the origin.

The web server intentionally listens only on localhost. On a remote machine,
forward it over SSH:

```bash
ssh -L 5000:127.0.0.1:5000 your-server
```

Then open <http://127.0.0.1:5000> on your own computer.

The deployed instance is available at <https://popcorn.devmindset.in>. It uses
local Jellyfin accounts as its identity source, so watch state remains private
and independent for every member. Administrators can create members in
**Settings → Members**; new accounts can search, download, and watch, but cannot
delete media or administer the server. Legacy single-user credentials in
`/etc/popcorn.env` remain supported during migration.

Popcorn is installable as a mobile Progressive Web App. On Android, use the
in-app **Install** button (or the browser's Install app action). On iPhone or
iPad, tap **Install**, then use Safari's **Share → Add to Home Screen**. The
installed app uses the standalone Popcorn shell while keeping authenticated
HTML and API data out of the offline cache.

To enable TMDB repair for posterless movies, add your TMDB v3 API key or v4 read
access token to `/etc/popcorn.env`, then restart Popcorn:

```ini
POPCORN_TMDB_API_KEY=paste_your_key_here
```

```bash
sudo systemctl restart popcorn
```

Use **Settings → Library → Repair missing posters** afterward. Jellyfin remains
the primary metadata provider; TMDB is only used to fill missing primary
posters.

### AI clips

Every film's detail page has **Generate clips**. Popcorn reads the film's
subtitles — or transcribes it, when it has none — asks a model which moments
would work as standalone short clips, sharpens each scene's start and end
against the picture and sound, and cuts them with ffmpeg. The work happens on
the server: close the tab and it carries on, and the clips are on the page the
next time you open it. The same page shows where the job has got to, and a
film already cut offers **Regenerate clips** instead.

Clips are saved to Drive beside the film they came from, in the same remote
downloads already use:

```
Movies/Inception (2010) [1080p]/
  Inception.2010.1080p.BrRip.x264.YIFY.mp4
  generated-clips/
    clips.json          # titles, hooks, scores, timings, subtitle data
    clip-001.mp4        # the clip itself
    clip-001.jpg        # preview frame
    clip-001.srt        # its subtitles, ready for burned-in captions later
    .ignore
```

The empty `.ignore` is what stops the clips turning up as extra "films" in
Jellyfin and in Popcorn's own grids: Jellyfin skips any folder that holds one.
A film stored outside the managed library — a loose file on another disk, say
— cannot have clips saved beside it, and says so rather than failing later.

Clips are cut by copying the film's own streams, so nothing is re-encoded and
no quality is lost. Only two things force an encode: a cut that cannot begin on
a keyframe (a copy would have to include everything back to the previous one),
and audio an MP4 will not carry, such as DTS — and then only the audio is
re-encoded.

The model is configured in `/etc/popcorn.env`:

```ini
POPCORN_LLM_BASE_URL=https://api.anthropic.com   # or any compatible endpoint
POPCORN_LLM_API_KEY=sk-ant-...
POPCORN_LLM_MODEL=claude-sonnet-5                # reads the transcript
POPCORN_VISION_MODEL=claude-haiku-4-5-20251001   # reviews the best scenes
POPCORN_LLM_PROXY=                               # optional egress proxy
```

**Settings → AI clips** sets how many clips a film gets, how long they may be,
whether the strongest candidates get a visual review, and which speech model to
use. The analysis — transcript, candidate scenes, scores — is remembered per
movie and keyed to the transcript and source file, so regenerating clips, or
changing the clip length, reuses it and never pays for the same reasoning
twice. `POST /api/clips/<item_id>/generate` accepts `{"refresh": true}` to
discard the stored analysis and ask the model again.

A film with no subtitles at all: Popcorn first asks your media server's
subtitle providers — Jellyfin's **Open Subtitles** plugin, once it is installed
and signed in, can find subtitles for almost anything. Whatever comes back is
checked against the film before it is believed, because a subtitle file is only
useful if its timings match the release it is played against:

- every subtitle is checked — downloaded, or a file already sitting beside the
  film. A few windows are sampled across the movie, the speech in each is found
  with a voice-activity detector (a level threshold is not enough: a film score
  reads as speech to one), and the subtitles are slid against it.
- a file whose timings match is used untouched, including hard-of-hearing
  subtitles and files that only cover part of the film.
- a file that is uniformly early or late is corrected and used, and the
  corrected timings are what gets saved — when the measurement is confident.
  On a film whose soundtrack is mostly music, the same evidence can be too
  thin to act on, and then the file is discarded rather than trusted.
- a file timed for a different frame rate (25 fps against 23.976, say) is
  corrected by rescaling when the measurement is confident enough, and
  otherwise discarded. This is best-effort: it is the hardest case to be sure
  of.
- a file that lines up with nothing in the film — subtitles for another
  release, or for a different cut of the same one — is discarded, removed from
  the media server, and the next candidate is tried. Nothing is ever guessed at:
  a subtitle is only corrected when several windows across the film agree on
  the same answer.

A subtitle that passes is saved beside the film on Drive as
`<film>.<lang>.srt`, which is where a subtitle belongs: Popcorn finds it for
free next time, any player can use it, and it survives anything happening to
this application. Jellyfin picks it up on its next rescan of that file rather
than immediately.

If no provider has anything, the film has to be listened to, which needs
speech-to-text on the server:

```bash
python3 -m pip install faster-whisper
```

Transcription is the slowest thing here by a wide margin: the default `small`
model runs at about 1.8× realtime on this server, so a two-hour film takes
roughly 70 minutes before any clip is cut. It is a background job with no one
waiting on it, and **Settings → AI clips → Speech model** trades that time for
accuracy (`base` is roughly twice as fast, `tiny` faster still). How many cores
it may use is set by `POPCORN_WHISPER_THREADS` (default 2, which leaves the
server responsive while it works).

**Settings → AI clips → Look for subtitles online** turns the provider search
off, and a subtitle already beside the film is never replaced by one.

Clip generation is CPU-bound, so one film runs at a time; a second request
joins the running job if it is for the same film, and is otherwise asked to
wait. Uploading clips counts against the same Google Drive quota as downloads.

### TV shows and episode previews

TV imports use a separate `gdrive:TV Shows` library mounted by
`deploy/popcorn-shows.service` at `/home/amal/gdrive-shows`. Movies continue to
use `gdrive:Movies`. Imports identify the show by TMDB ID and organize each
video under `Show (year) [tmdbid-ID]/Season XX/Show SxxExx - version.ext`.
Separate releases join the same series and season; version suffixes preserve
files from different sources. Matching subtitle sidecars keep their language
and accessibility suffixes.

The download picker always displays release size and offers automatic, movie,
or TV classification. For TV, **Find show** lets you choose the provider's
record and optionally specify a season when filenames omit it. Automatic
matching accepts one exact normalized title match. Ambiguous identities,
season numbers, or specials stay local with an **Identify show** action in
Downloads; they are never uploaded as a guessed movie. Recovery keeps the
original downloaded files until the normalized upload is committed.

TV metadata, posters, backdrops and episode stills are saved alongside media,
so the read-only mount does not need Jellyfin to fetch external metadata.
`media_library.py` fetches bounded HTTPS requests with verified DNS fallback,
keeps a persistent metadata cache, and never puts credentials in command-line
arguments or diagnostics. The TV library reads those local metadata files.

The player now shows scrub thumbnails from the negotiated media version.
Existing thumbnails for migrated TV files are retained locally, and missing
ones can be generated with `deploy/tv-library.py generate-missing-previews`.
That command uses the existing Jellyfin ffmpeg installation. Image routes
remain authenticated. The initial migration's file map and preview cache are
under `data/tv-migration`; retain them while its preserved previews are used.

Run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`,
`node tests/test_search.mjs`, and `node tests/test_previews.mjs` for validation.

### Fixing mismatched titles

Titles downloaded as release folders — `www.UIndex.org - Balan The Boy 2026
1080p ZEE5 WEB-DL...` — often arrive with no description, genres or backdrop,
because Jellyfin could not match the folder name to a film. **Settings →
Library → Check for missing details** asks your metadata provider what each one
probably is and shows you the proposed match before anything changes; nothing
is applied until you confirm. Titles that look like TV episodes filed as films
are listed separately rather than matched to the wrong thing.

Popcorn also shortens release names for display everywhere in its own
interface, so `www.5MovieRulz.tips - Sapta Sagaradaache Ello - Side A` reads as
`Sapta Sagaradaache Ello - Side A`. The library record itself is never rewritten
by this — that is what the metadata matching above is for.

Completed downloads go to `gdrive:Movies` by default. The temporary local
download directory is removed after a successful rclone upload; if a download
or upload fails it is retained so the data is recoverable.

Download progress and job checkpoints are persisted in
`data/popcorn.sqlite3`. Reloading the browser restores active progress polling
and completed/failed history for the signed-in member; progress updates never
change the page scroll. Terminal jobs clear transfer speed and ETA. Completed
uploads refresh Jellyfin through a dedicated server API key, so a browser
logout or a newer login cannot interrupt library discovery. A completed upload
stays in "Almost ready" until Jellyfin exposes the exact media path, at which
point it becomes **Ready to watch** and its Play action opens that film
directly. History entries can be removed without deleting their media.
Active downloads appear under **Downloads** and can be stopped by their owner;
stopping terminates the whole aria2/rclone process group and removes partial
local data. Terminal jobs move to the **History** tab. A failed or interrupted
download offers **Find again**, pre-filled with the same search. New jobs resume
their original aria2 state after restart. Failed uploads retry from the finished
local files; indexing retries without downloading or uploading again. Pending
retries offer **Retry now**. Stopping an upload retains its completed local copy.

See [source reliability and configuration](docs/SOURCE_RELIABILITY.md) for the
architecture, every environment option, migration behavior, and remaining
limitations. Operational defaults require no additional configuration. Web
searches use the host resolver without modifying process-wide DNS.

Run the failure tests with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`.
They include provider failures, cache/circuit recovery, upload/index isolation,
subprocess reattachment, and actual aria2 resume with generated localhost data.

The **Watch** page embeds the local Jellyfin service after Popcorn silently
creates the matching Jellyfin session. Jellyfin reads a read-only
`gdrive:Movies` mount at `/home/amal/gdrive-movies`; its rclone VFS cache is
capped at 64 GB and retains hot media for up to 72 hours. The mount uses
read-ahead and growing range requests for fast starts and seeks. Its local-only
remote-control endpoint lets uploads and deletions invalidate directory caches
immediately. Embedded and
sidecar subtitles are enabled in Jellyfin, while preferred language and
behavior live in Popcorn's **Settings** tab. Jellyfin trickplay images are
enabled for timeline hover previews and generated after new uploads. Deployment
units, Jellyfin library options, and Nginx configuration are in [`deploy/`](deploy/).

`aria2c` is required for downloads, and `rclone` is required only for remote
destinations. Use the application responsibly and follow copyright law in your
region.

## CLI

```bash
python3 popcorn.py "Inception 2010"
```

Run `python3 popcorn.py --help` for all source, proxy, DNS, and upload options.


### Automatic subtitle downloads

Administrators can connect an OpenSubtitles.com account in **Settings → Subtitle
downloads**. Supply a username, password and your API consumer key, then choose
**Connect and enable**. The account is verified before saving. Connection enables
subtitle fetching for new downloads and starts a background check of existing
movies and episodes. **Find subtitles for existing videos** runs that check again
after a provider outage or quota reset. Provider limits and subtitle availability
still apply; paid transcription/translation is not invoked.

The preferred language in Playback controls which subtitles are fetched. Files
with full subtitles in that language are skipped. Matching first uses the exact
video hash, then the movie/show identity, episode numbers and release name.
Combined episodes require an exact hash match. Uncertain release matches are
left without new subtitles rather than silently attaching another cut's timing.

New subtitle files use the video's basename and language suffix (for example
`Example S01E02.eng.srt`), and are uploaded alongside the video. Existing library
checks upload just the subtitle file to the matching Drive folder, then refresh
the library. Video files and existing subtitle files are not overwritten.
A subtitle provider error never prevents a new video from completing its upload.
The download history and Settings show subtitle results and quota failures.

Credentials are stored in `data/subtitles/account.json`, with permissions 0600,
and excluded from Git. API responses and ordinary settings do not expose the
password or key. Alternatively configure `POPCORN_OPENSUBTITLES_USERNAME`,
`POPCORN_OPENSUBTITLES_PASSWORD` and `POPCORN_OPENSUBTITLES_API_KEY` in the service
environment. No OpenSubtitles account is preconfigured by this deployment.
