"""The clip job itself: transcript in, uploaded clips out.

The stage order is the whole design. Subtitles are read first because they are
nearly free; the model sees text only, never the film; the picture is touched
twice — once cheaply, to find where a scene really starts and ends, and once to
cut the clip. Nothing is ever sent to a model that the transcript could have
answered.

The expensive middle of the job is cached per movie (see `clips.store`), so
regenerating clips, or rendering a second profile later, does not ask a model
to understand the film again.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import analysis, render, store, sync, transcript as transcripts

# Bumped when a change to prompts or scoring would produce different clips from
# the same film; a stored analysis from an older version is re-run.
PIPELINE_VERSION = 1
ANALYSIS_VERSION = 1

# Candidates that get the (cheap) media-signal treatment and the (optional)
# vision call. Everything else is ranked straight from the transcript scores.
REFINE_LIMIT = 20
VISION_LIMIT = 12

MANIFEST_NAME = "clips.json"
IGNORE_NAME = ".ignore"
TEMP_SUBDIR = "_tmp"

# Status names, in the order a job passes through them.
QUEUED = "QUEUED"
EXTRACTING_SUBTITLES = "EXTRACTING_SUBTITLES"
TRANSCRIBING = "TRANSCRIBING"
ANALYZING_TRANSCRIPT = "ANALYZING_TRANSCRIPT"
FINDING_SCENES = "FINDING_SCENES"
RANKING_SCENES = "RANKING_SCENES"
GENERATING_CLIPS = "GENERATING_CLIPS"
UPLOADING = "UPLOADING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

STAGE_LABELS = {
    QUEUED: "Queued",
    EXTRACTING_SUBTITLES: "Finding subtitles",
    TRANSCRIBING: "Listening to the film",
    ANALYZING_TRANSCRIPT: "Reading the transcript",
    FINDING_SCENES: "Finding scenes",
    RANKING_SCENES: "Choosing the best clips",
    GENERATING_CLIPS: "Generating clips",
    UPLOADING: "Saving clips",
    COMPLETED: "Completed",
    FAILED: "Failed",
    CANCELLED: "Stopped",
}

DEFAULT_SETTINGS = {
    "clip_target_count": 12,
    "clip_min_seconds": 20,
    "clip_max_seconds": 75,
    "clip_vision": True,
    # Ask the media server's providers for subtitles when a film has none.
    "clip_subtitle_download": True,
    "clip_whisper_model": "small",
}


class ClipJobCancelled(Exception):
    """The viewer stopped the job."""


@dataclass
class ClipMedia:
    """Everything the pipeline needs about one movie, resolved by the caller."""

    item_id: str
    title: str
    source_path: str          # the movie file, on this server
    clips_subdir: str         # clip folder relative to the library mount
    remote_dir: str           # rclone destination for that folder
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    runtime_seconds: float | None = None


@dataclass
class Hooks:
    """How the pipeline talks to the job it is running inside.

    The pipeline never touches the job registry itself: it reports stages and
    progress, asks whether it has been cancelled, and spawns subprocesses
    through the caller so the Stop button can kill them.
    """

    on_stage: object            # (status, message)
    on_progress: object         # (done, total, label)
    is_cancelled: object        # () -> bool
    spawn: object               # (command, **kwargs) -> Popen
    refresh_mount: object       # () -> None
    work_dir: Path
    log: object = None          # (message) -> None

    def note(self, message: str) -> None:
        if self.log:
            self.log(message)

    def stage(self, status: str, message: str = "") -> None:
        self.on_stage(status, message or STAGE_LABELS.get(status, status))

    def progress(self, done: int, total: int, label: str = "") -> None:
        self.on_progress(done, total, label)

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise ClipJobCancelled()


# ── Stage 1: the media, and what it says ────────────────────────────────────


def _resolve_source(media: ClipMedia, hooks: Hooks) -> str:
    """The movie file must be readable here; a stale mount is refreshed once."""
    path = Path(media.source_path)
    if path.exists():
        return str(path)
    hooks.note("The library mount did not have this file yet; refreshing it.")
    hooks.refresh_mount()
    if path.exists():
        return str(path)
    raise RuntimeError(
        "This movie's file is not available on the server right now, so clips cannot be cut from it."
    )


def _jellyfin_transcript(media: ClipMedia, hooks: Hooks, subtitle_source,
                         duration: float | None, preferred: str):
    """Subtitle tracks Jellyfin already knows about, including embedded ones."""
    if subtitle_source is None:
        return None
    try:
        tracks = subtitle_source.tracks(media.item_id)
    except Exception as exc:  # a media-server hiccup must not fail the job
        hooks.note(f"Could not list subtitle tracks: {exc}")
        return None

    tracks = sorted(tracks, key=lambda track: (
        0 if track.get("language") == preferred else 1 if track.get("language") in (None, "en") else 2,
        0 if track.get("is_default") else 1,
    ))
    for track in tracks:
        try:
            text = subtitle_source.fetch(media.item_id, track)
        except Exception as exc:
            hooks.note(f"Could not read subtitle track {track.get('index')}: {exc}")
            continue
        if not text:
            continue
        segments = transcripts.parse_subtitle(text)
        if not segments:
            continue
        candidate = transcripts.Transcript(
            segments, "jellyfin", track.get("title") or f"track {track.get('index')}",
            track.get("language"),
        )
        ok, reason = transcripts.usable(candidate, duration)
        if ok:
            return candidate
        hooks.note(f"Skipping subtitle track {track.get('index')}: {reason}.")
    return None


def _embedded_transcript(media: ClipMedia, hooks: Hooks, duration: float | None,
                         preferred: str):
    """Text subtitle tracks read straight out of the container."""
    source = media.source_path
    for track in transcripts.probe_text_subtitle_streams(source):
        hooks.check_cancelled()
        segments = transcripts.extract_embedded(source, track["position"])
        if not segments:
            continue
        candidate = transcripts.Transcript(
            segments, "embedded", track.get("title") or f"{track.get('codec')} track",
            track.get("language"),
        )
        ok, reason = transcripts.usable(candidate, duration)
        if ok:
            return candidate
        hooks.note(f"Skipping embedded track {track['position']}: {reason}.")
    return None


def _checked(source: str, transcript, duration: float | None, hooks: Hooks):
    """Confirm a text transcript lines up with the film, and fix it if it can be.

    Subtitle files found online are routinely timed for a different release, and
    the clip boundaries are taken from these timings — so an out-of-sync file
    would put every clip in the wrong place, and be saved beside the film as a
    wrong subtitle too.
    """
    verdict = sync.verify(source, transcript.segments, duration, on_note=hooks.note)
    hooks.note(f"Sync check on {transcript.detail}: {verdict.describe()}.")
    if verdict.status == sync.UNUSABLE:
        return None
    if verdict.status == sync.CORRECTED:
        transcript.segments = sync.corrected(transcript.segments, verdict, duration)
        transcript.corrected = True
    return transcript


def _downloaded_transcript(media: ClipMedia, hooks: Hooks, settings: dict, source: str,
                           duration: float | None, preferred: str, subtitle_source,
                           uploader):
    """Ask the media server's subtitle providers, and keep what they return.

    The subtitle is applied through the media server (so it can be played back
    as well), checked against the film's speech, and — if it lines up — written
    beside the movie on Drive, where it belongs to the film rather than to this
    application.
    """
    if subtitle_source is None or not settings.get("clip_subtitle_download", True):
        return None
    hooks.stage(EXTRACTING_SUBTITLES, "No subtitles on file. Looking online…")
    try:
        candidates = subtitle_source.search(media.item_id, preferred)
    except Exception as exc:  # a provider outage must not fail the job
        hooks.note(f"Could not search for subtitles: {exc}")
        return None
    if not candidates:
        hooks.note(
            "No subtitle provider offered anything for this film "
            "(the media server may have none configured or signed in)."
        )
        return None

    ranked = _rank_subtitle_candidates(candidates, media.source_path, preferred)
    hooks.note(f"{len(candidates)} candidate subtitles found; trying the best {min(3, len(ranked))}.")
    for candidate in ranked[:3]:
        hooks.check_cancelled()
        known = {track.get("index") for track in _safe_tracks(subtitle_source, media.item_id)}
        if not subtitle_source.apply(media.item_id, candidate.get("id")):
            continue
        track = _await_track(subtitle_source, media.item_id, known, candidate, hooks)
        if track is None:
            hooks.note(f"“{candidate.get('name')}” did not attach to the film; skipping it.")
            continue
        text = subtitle_source.fetch(media.item_id, track)
        segments = transcripts.parse_subtitle(text or "")
        if not segments:
            subtitle_source.remove(media.item_id, track)
            continue
        transcript = transcripts.Transcript(
            segments, "downloaded", candidate.get("name") or "downloaded subtitle",
            candidate.get("language"),
        )
        usable, reason = transcripts.usable(transcript, duration)
        if not usable:
            hooks.note(f"“{transcript.detail}” was not usable: {reason}.")
            subtitle_source.remove(media.item_id, track)
            continue
        checked = _checked(source, transcript, duration, hooks)
        if checked is None:
            hooks.note(f"“{transcript.detail}” does not match this release; discarding it.")
            subtitle_source.remove(media.item_id, track)
            continue
        _keep_subtitle(media, hooks, uploader, checked, preferred)
        return checked
    return None


def _safe_tracks(subtitle_source, item_id: str) -> list[dict]:
    try:
        return subtitle_source.tracks(item_id)
    except Exception:
        return []


def _await_track(subtitle_source, item_id: str, known, candidate: dict, hooks: Hooks,
                 attempts: int = 4) -> dict | None:
    """The track the provider just added, once the media server has it."""
    for attempt in range(attempts):
        if attempt:
            time.sleep(1.5)
        for track in _safe_tracks(subtitle_source, item_id):
            if track.get("index") in known:
                continue
            if candidate.get("language") and track.get("language") not in (None, candidate["language"]):
                continue
            return track
    return None


def _keep_subtitle(media: ClipMedia, hooks: Hooks, uploader, transcript, preferred: str) -> None:
    """Write the checked subtitle beside the film, so playback gains it too."""
    name = f"{Path(media.source_path).stem}.{preferred}.srt"
    try:
        if uploader.store_subtitle(media, name, transcript.segments):
            hooks.note(f"Saved {name} beside the film.")
        else:
            hooks.note(f"{name} is already beside the film; left as it is.")
    except Exception as exc:  # storage trouble must not fail the job
        hooks.note(f"The subtitle could not be saved beside the film: {exc}")


def _rank_subtitle_candidates(candidates: list[dict], source_path: str, preferred: str) -> list[dict]:
    """Best first: an exact release match, then the busiest, then by format."""
    stem = Path(source_path).stem.lower()
    release_words = {word for word in re.split(r"[^a-z0-9]+", stem) if len(word) > 2}

    def score(candidate: dict) -> tuple:
        name = (candidate.get("name") or "").lower()
        overlap = len(release_words & {word for word in re.split(r"[^a-z0-9]+", name) if len(word) > 2})
        return (
            1 if candidate.get("language") == preferred else 0,
            1 if candidate.get("hash_match") else 0,
            overlap,
            int(candidate.get("downloads") or 0),
            1 if (candidate.get("format") or "").lower().startswith("srt") else 0,
        )

    return sorted(candidates, key=score, reverse=True)


def _build_transcript(media: ClipMedia, hooks: Hooks, settings: dict,
                      subtitle_source, uploader, duration: float | None):
    """Stages 1-2: get a timestamped transcript, by the cheapest route available."""
    preferred = (settings.get("subtitle_language") or "eng")[:2]

    hooks.stage(EXTRACTING_SUBTITLES, "Looking for subtitles…")
    sidecar = transcripts.find_sidecar(Path(media.source_path), preferred, log=hooks.note)
    if sidecar:
        ok, reason = transcripts.usable(sidecar, duration)
        if ok:
            checked = _checked(media.source_path, sidecar, duration, hooks)
            if checked is not None:
                return checked
            hooks.note("The subtitle file next to this movie is for a different release.")
        else:
            hooks.note(f"The subtitle file next to this movie was not usable: {reason}.")

    from_jellyfin = _jellyfin_transcript(media, hooks, subtitle_source, duration, preferred)
    if from_jellyfin:
        checked = _checked(media.source_path, from_jellyfin, duration, hooks)
        if checked is not None:
            return checked
        hooks.note("The subtitle track on the media server is for a different release.")

    embedded = _embedded_transcript(media, hooks, duration, preferred)
    if embedded:
        # Embedded subtitles are timed against this very file, so they are not
        # worth spending the measurement on.
        return embedded

    downloaded = _downloaded_transcript(
        media, hooks, settings, media.source_path, duration, preferred, subtitle_source,
        uploader,
    )
    if downloaded:
        return downloaded

    # Nothing written down: the film has to be listened to.
    hooks.stage(TRANSCRIBING, "No subtitles found. Transcribing the film — this takes a while…")
    audio = transcripts.extract_audio(
        media.source_path, hooks.work_dir / TEMP_SUBDIR, spawn=hooks.spawn,
        duration=duration,
        on_progress=lambda done, total, label: hooks.progress(int(done), int(total), label),
    )
    hooks.check_cancelled()
    return transcripts.transcribe_with_whisper(
        audio,
        model_size=str(settings.get("clip_whisper_model") or "small"),
        cpu_threads=max(1, int(os.environ.get("POPCORN_WHISPER_THREADS", "2"))),
        duration=duration,
        language=None,
        on_progress=lambda done, total, label: hooks.progress(int(done), int(total), label),
        on_note=hooks.note,
    )


# ── Stage 3-6: choosing the clips ───────────────────────────────────────────


def _source_hash(path: str) -> str:
    """Cheap identity for the file: rewriting or replacing it changes this."""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def _refine(candidates: list[dict], segments: list, source: str, *,
            duration: float | None, hooks: Hooks, min_seconds: float, max_seconds: float) -> list[dict]:
    """Stage 4: pull each candidate's boundaries onto real cut points.

    The candidates most likely to survive ranking are refined first, and the
    result keeps that order: the vision pass below only looks at the head of
    the list.
    """
    ordered = sorted(candidates, key=lambda item: item.get("interest_score", 50), reverse=True)
    strong, rest = ordered[:REFINE_LIMIT], ordered[REFINE_LIMIT:]
    refined: list[dict] = []

    for index, candidate in enumerate(strong):
        hooks.check_cancelled()
        hooks.progress(index, len(strong), f"Finding scene boundaries ({index + 1} of {len(strong)})")
        signals = render.gather_signals(source, candidate["start"], candidate["end"], on_note=hooks.note)
        refined.append(render.refine_bounds(
            candidate, segments, signals, duration=duration,
            min_seconds=min_seconds, max_seconds=max_seconds,
        ))
    # Anything past the refine limit keeps its subtitle-derived bounds; it is
    # only there to fill the target if the strong candidates disappoint.
    for candidate in rest:
        fallback = dict(candidate)
        fallback.update({"duration": round(fallback["end"] - fallback["start"], 3), "boundary_quality": 0.5})
        refined.append(fallback)
    return refined


def _bounds_within(refined: list[dict], min_seconds: float, max_seconds: float) -> bool:
    """Whether stored boundaries still respect the configured clip length."""
    for candidate in refined:
        duration = candidate.get("duration") or (candidate["end"] - candidate["start"])
        if duration < min_seconds * 0.9 or duration > max_seconds * 1.1:
            return False
    return True


def _visual_pass(refined: list[dict], source: str, hooks: Hooks, config) -> bool:
    """Stage 5: let a vision model look at the strongest candidates only.

    Returns whether any scores came back, so the caller can tell a pass that
    ran from one that had nothing to say.
    """
    if not config.vision_configured:
        hooks.note("Visual review is unavailable; ranking on the transcript alone.")
        return False
    temp = hooks.work_dir / TEMP_SUBDIR
    temp.mkdir(parents=True, exist_ok=True)
    shots = []
    for index, candidate in enumerate(refined[:VISION_LIMIT]):
        hooks.check_cancelled()
        image = temp / f"sheet-{index:02d}.jpg"
        if render.build_contact_sheet(source, image, candidate["start"], candidate["duration"]):
            shots.append({
                "key": f"{candidate['start']:.2f}-{candidate['end']:.2f}",
                "image": str(image),
                "label": candidate.get("title") or "",
            })
    if len(shots) < 2:
        hooks.note("Visual review skipped: the scenes could not be sampled.")
        return False
    hooks.progress(0, len(shots), "Reviewing how the strongest scenes look")
    ratings = analysis.visual_review(shots, config, on_note=hooks.note)
    for candidate in refined:
        key = f"{candidate['start']:.2f}-{candidate['end']:.2f}"
        if key in ratings:
            candidate["visual_score"] = ratings[key]
    return bool(ratings)


def _carry_visual(old: list[dict], fresh: list[dict]) -> None:
    """Move stored visual scores onto re-refined candidates.

    Only the vision call is worth keeping when the boundaries are recomputed:
    it is the one part of the analysis that cannot be derived again for free.
    """
    scores = {
        (candidate.get("raw_start"), candidate.get("raw_end")): candidate["visual_score"]
        for candidate in old
        if candidate.get("visual_score") is not None
        and candidate.get("raw_start") is not None
        and candidate.get("raw_end") is not None
    }
    for candidate in fresh:
        score = scores.get((candidate.get("raw_start"), candidate.get("raw_end")))
        if score is not None:
            candidate["visual_score"] = score


def _analyse(media: ClipMedia, transcript, source: str, *, duration: float | None,
             settings: dict, hooks: Hooks, config) -> tuple[list[dict], list[dict], bool]:
    """Stages 3-6, reusing a stored analysis whenever the inputs are unchanged."""
    transcript_hash = transcript.digest
    source_hash = _source_hash(source)
    cached = store.load_analysis(media.item_id)

    min_seconds = float(settings.get("clip_min_seconds", 20))
    max_seconds = float(settings.get("clip_max_seconds", 75))

    if (
        cached
        and cached.get("analysis_version") == ANALYSIS_VERSION
        and cached.get("transcript_hash") == transcript_hash
        and cached.get("source_hash") == source_hash
        and cached.get("refined")
    ):
        hooks.note("Analysis cache hit: no model calls needed for this run.")
        candidates, refined = cached["candidates"], cached["refined"]
        if not _bounds_within(refined, min_seconds, max_seconds):
            # Clip length changed since this analysis was made. Re-cut the
            # boundaries from the same candidates — media signals only, still
            # no model calls — and keep the visual scores already paid for.
            hooks.stage(FINDING_SCENES, "Re-fitting scene boundaries to the new clip length…")
            refreshed = _refine(
                candidates, [transcripts.Segment(s["start"], s["end"], s["text"])
                             for s in cached.get("transcript") or []],
                source, duration=duration, hooks=hooks,
                min_seconds=min_seconds, max_seconds=max_seconds,
            )
            _carry_visual(refined, refreshed)
            refined = refreshed
        else:
            hooks.stage(FINDING_SCENES, "Reusing the scenes found for this movie last time.")

        # The vision pass is cheap next to the transcript analysis and may have
        # been off — or failed — when this analysis was stored, so it is given
        # another chance rather than being skipped for the movie's lifetime.
        if settings.get("clip_vision", True) and not any(
            candidate.get("visual_score") is not None for candidate in refined
        ):
            hooks.stage(RANKING_SCENES, "Checking how the strongest scenes look…")
            if _visual_pass(refined, source, hooks, config):
                store.save_analysis(
                    item_id=media.item_id, analysis_version=ANALYSIS_VERSION,
                    source_path=source, source_hash=source_hash,
                    duration=float(duration or 0), transcript_hash=transcript_hash,
                    transcript_source=transcript.source, transcript_detail=transcript.detail,
                    transcript=cached.get("transcript") or transcript.as_list(),
                    candidates=candidates, refined=refined,
                )
        return candidates, refined, True

    hooks.stage(ANALYZING_TRANSCRIPT, "Asking the model which moments are worth clipping…")
    meta = {
        "title": media.title,
        "year": media.year,
        "genres": media.genres,
        "runtime_seconds": duration,
        "overview": None,
    }
    hooks.progress(0, 1, "Reading the transcript")
    candidates, complete = analysis.find_candidates(
        transcript, meta, config, on_progress=hooks.progress, on_note=hooks.note,
    )
    hooks.check_cancelled()
    if not candidates:
        raise RuntimeError("No moment in this film looked worth clipping.")

    hooks.stage(FINDING_SCENES, "Improving where each scene starts and ends…")
    refined = _refine(
        candidates, transcript.segments, source, duration=duration, hooks=hooks,
        min_seconds=min_seconds, max_seconds=max_seconds,
    )

    if settings.get("clip_vision", True):
        hooks.stage(RANKING_SCENES, "Checking how the strongest scenes look…")
        _visual_pass(refined, source, hooks, config)
    else:
        hooks.stage(RANKING_SCENES, "Scoring the candidates…")

    if complete:
        store.save_analysis(
            item_id=media.item_id,
            analysis_version=ANALYSIS_VERSION,
            source_path=source,
            source_hash=source_hash,
            duration=float(duration or 0),
            transcript_hash=transcript_hash,
            transcript_source=transcript.source,
            transcript_detail=transcript.detail,
            transcript=transcript.as_list(),
            candidates=candidates,
            refined=refined,
        )
    else:
        # A half-read transcript must not be remembered as the finished
        # article: the clips from it are worth keeping, the cache is not.
        hooks.note(
            "Part of the transcript could not be read, so this analysis was not saved; "
            "the next run will read the whole film again."
        )
    return candidates, refined, False


# ── Stage 7-8: cutting the clips ────────────────────────────────────────────


def _clip_id(item_id: str, position: int) -> str:
    return f"{item_id[:8]}-{position:02d}-{uuid.uuid4().hex[:8]}"


def _render_clips(selected: list[dict], transcript, source: str, profile, *,
                  media: ClipMedia, hooks: Hooks, info: dict) -> list[dict]:
    """Cut every selected clip, with a thumbnail and its subtitles alongside."""
    work = hooks.work_dir
    produced: list[dict] = []
    failures = 0

    for index, candidate in enumerate(selected):
        hooks.check_cancelled()
        position = index + 1
        hooks.progress(index, len(selected), f"Generating clip {position} of {len(selected)}")
        stem = f"clip-{position:03d}"
        target = work / f"{stem}.mp4"
        start, end = candidate["start"], candidate["end"]

        try:
            rendered = render.cut_clip(
                source, target, start, end, profile,
                has_audio=bool(info.get("audio_codec")),
                audio_index=int(info.get("audio_index") or 0),
                audio_playable=bool(info.get("audio_playable")),
                on_note=hooks.note,
            )
        except render.RenderError as exc:
            failures += 1
            hooks.note(f"Clip {position} could not be cut: {exc}")
            continue

        # A stream copy can begin on an earlier keyframe than requested, so the
        # clip's real start — not the planned one — is what gets recorded and
        # what the subtitles are timed against.
        actual_start = rendered["actual_start"]
        duration = rendered["duration"]
        segments = transcript.rebased(actual_start, actual_start + duration)
        thumbnail = work / f"{stem}.jpg"
        if not render.make_thumbnail(source, thumbnail, actual_start + min(2.0, duration / 3)):
            thumbnail = None
        srt = work / f"{stem}.srt"
        if segments and not render.write_srt(segments, srt):
            srt = None

        produced.append({
            **candidate,
            "clip_id": _clip_id(media.item_id, position),
            "position": position,
            "start": actual_start,
            "end": round(actual_start + duration, 3),
            "duration": duration,
            "file_name": target.name,
            "thumb_name": thumbnail.name if thumbnail else None,
            "srt_name": srt.name if srt else None,
            "size_bytes": rendered["size"],
            "render_mode": rendered["mode"],
            "subtitle_segments": segments,
            "profile": profile.name,
            "processing_version": PIPELINE_VERSION,
            "created_at": time.time(),
        })

    if failures and not produced:
        raise render.RenderError("None of the selected clips could be cut from this file.")
    if failures:
        hooks.note(f"{failures} of {len(selected)} clips could not be cut and were skipped.")
    hooks.progress(len(produced), len(selected), f"Generated {len(produced)} of {len(selected)}")
    return produced


def _manifest(media: ClipMedia, transcript, clips: list[dict], *, duration: float | None,
              reused: bool) -> dict:
    """The record that travels with the clips, so the folder explains itself."""
    return {
        "movie": {
            "item_id": media.item_id,
            "title": media.title,
            "year": media.year,
            "runtime_seconds": duration,
        },
        "pipeline_version": PIPELINE_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "analysis_reused": reused,
        "transcript": {
            "source": transcript.source,
            "detail": transcript.detail,
            "language": transcript.language,
            "hash": transcript.digest,
            "segments": len(transcript.segments),
        },
        "source_file": media.source_path,
        "generated_at": time.time(),
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clips": [
            {
                "id": clip["clip_id"],
                "index": clip["position"],
                "file": clip["file_name"],
                "thumbnail": clip.get("thumb_name"),
                "subtitles": clip.get("srt_name"),
                "title": clip["title"],
                "summary": clip.get("summary"),
                "hook": clip.get("hook"),
                "category": clip.get("category"),
                "reason": clip.get("reason"),
                "start": clip["start"],
                "end": clip["end"],
                "duration": clip["duration"],
                "scores": {
                    "interest": clip.get("interest_score"),
                    "context": clip.get("context_score"),
                    "dialogue": clip.get("dialogue_score"),
                    "visual": clip.get("visual_score"),
                    "final": clip.get("final_score"),
                },
                "boundary_notes": clip.get("boundary_notes", []),
                "profile": clip.get("profile"),
                "processing_version": clip.get("processing_version"),
            }
            for clip in clips
        ],
    }


# ── Stage 9: persistence ────────────────────────────────────────────────────


def _upload(media: ClipMedia, hooks: Hooks, manifest: dict, uploader) -> None:
    """Hand the finished folder to the caller's storage, which owns rclone."""
    hooks.stage(UPLOADING, f"Saving {len(manifest['clips'])} clips alongside the movie…")
    (hooks.work_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    # An empty .ignore makes the media server skip this folder entirely: the
    # clips are for Popcorn and the social feeds, not extra "movies" in the
    # library. Jellyfin honours it (Emby.Server.Implementations DotIgnoreIgnoreRule).
    (hooks.work_dir / IGNORE_NAME).write_text("", encoding="utf-8")
    uploader.upload(hooks.work_dir, media.remote_dir, hooks)


def run_clip_job(*, media: ClipMedia, settings: dict, hooks: Hooks,
                 uploader, subtitle_source=None, config=None) -> dict:
    """Run the whole pipeline for one movie.

    Raises `ClipJobCancelled` when the viewer stopped it, and any other
    exception to fail the job. Local working files are always cleaned up.
    """
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    config = config or analysis.LLMConfig.from_env()
    profile = render.PROFILES[render.DEFAULT_PROFILE]
    started = time.time()
    reused = False

    try:
        hooks.check_cancelled()
        source = _resolve_source(media, hooks)
        info = render.probe(source)
        duration = info.get("duration") or media.runtime_seconds
        hooks.note(
            f"Source: {info.get('video_codec') or '?'} {info.get('width')}x{info.get('height')}, "
            f"audio {info.get('audio_codec') or 'none'}"
            f"{'' if info.get('audio_playable') else ' (will be converted for playback)'}, "
            f"{duration or 0:.0f}s."
        )

        transcript = _build_transcript(
            media, hooks, settings, subtitle_source, uploader, duration
        )
        hooks.note(
            f"Transcript: {len(transcript.segments)} cues from {transcript.source} "
            f"({transcript.detail})."
        )

        candidates, refined, reused = _analyse(
            media, transcript, source, duration=duration, settings=settings,
            hooks=hooks, config=config,
        )
        selected = analysis.rank_candidates(
            refined,
            duration=duration,
            target_count=int(settings.get("clip_target_count", 12)),
        )
        hooks.note(f"{len(candidates)} candidates, {len(refined)} refined, {len(selected)} selected.")
        if not selected:
            raise RuntimeError("No clip scored well enough to be worth generating.")

        hooks.stage(GENERATING_CLIPS, f"Generating 0 of {len(selected)}")
        clips = _render_clips(
            selected, transcript, source, profile, media=media, hooks=hooks, info=info,
        )
        for clip in clips:
            clip["local_dir"] = media.clips_subdir

        _upload(media, hooks, _manifest(media, transcript, clips, duration=duration, reused=reused), uploader)
        store.replace_clips(media.item_id, clips)
        store.set_analysis_clips_dir(media.item_id, media.clips_subdir)

        return {
            "clips": len(clips),
            "requested": len(selected),
            "reused_analysis": reused,
            "transcript_source": transcript.source,
            "transcript_detail": transcript.detail,
            "elapsed_seconds": round(time.time() - started, 1),
            "profile": profile.name,
        }
    finally:
        shutil.rmtree(hooks.work_dir, ignore_errors=True)
