"""Cutting clips out of a movie, and making the cuts land in the right place.

A subtitle timestamp says roughly where a scene is, never exactly where a clip
should start: it lands mid-shot, mid-breath, or a beat before the line that
makes the scene. So every candidate is refined against cheap media signals
before it is cut — keyframes, silence, scene changes and the subtitle
boundaries themselves — and the clip is then cut without re-encoding whenever
the container allows it.

Rendering is expressed as a profile so a vertical or captioned rendition can be
added later without touching the pipeline: `original` is the only profile
registered today, and it copies the source streams untouched.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ffmpeg is asked to scan only a short window around each candidate, so the
# refinement below costs a few seconds of decode per candidate, not per film.
SIGNAL_WINDOW = 12.0
SILENCE_NOISE = "-30dB"
SILENCE_MINIMUM = 0.35
SCENE_THRESHOLD = 0.35
# How far a boundary may be nudged to land on a keyframe, a silence or a cut.
START_TOLERANCE = 2.0
END_TOLERANCE = 2.5
# A stream copy can only begin on a keyframe. Beyond this much lead-in the
# clip would gain a visible chunk of the previous scene, so it is encoded
# exactly instead.
COPY_KEYFRAME_TOLERANCE = 2.0
TAIL = 0.7          # let the last line finish before cutting away
LEAD_IN = 0.35      # a beat of air before the first line
# How far back a clip may be moved to start at the beginning of a line rather
# than in the middle of one. A long subtitle can have been on screen for a
# while when the scene starts; following it back would change the scene.
LINE_LOOKBACK = 4.0


class RenderError(Exception):
    """The source could not be read or a clip could not be produced."""


@dataclass(frozen=True)
class RenderProfile:
    """How a clip is rendered.

    `video_filter`/`audio_filter` are ffmpeg filter chains applied only when
    the streams must be re-encoded; `force_encode` exists for a profile whose
    filters cannot be applied to a copy, such as a vertical crop.
    """

    name: str
    video_filter: str | None = None
    audio_filter: str | None = None
    force_encode: bool = False
    requires_vision: bool = False  # a cropped profile needs to know where faces are

    @property
    def copies(self) -> bool:
        return not self.force_encode and self.video_filter is None


PROFILES: dict[str, RenderProfile] = {
    # The film's own frame, stream-copied: no quality loss, no encoding time.
    "original": RenderProfile("original"),
}
DEFAULT_PROFILE = "original"

# Sound that a browser can actually play inside an MP4. The source's own track
# is copied when it is one of these; anything else (E-AC-3, AC-3, DTS, TrueHD)
# is re-encoded, because a clip whose audio the browser cannot decode plays
# silently — which looks exactly like a broken clip, and did.
PLAYABLE_AUDIO = {"aac", "mp3"}


# ── Probing ─────────────────────────────────────────────────────────────────


def _run(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RenderError(f"{command[0]} is not installed on this server.") from exc
    except subprocess.SubprocessError as exc:
        raise RenderError(f"{command[0]} failed: {exc}") from exc


def probe(path: str) -> dict:
    """Duration, picture and sound of the source file, in one pass."""
    result = _run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path,
    ], timeout=180)
    if result.returncode != 0:
        raise RenderError("This movie file could not be read.")
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise RenderError("This movie file could not be read.") from exc

    video = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {})
    audio_streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
    # The track a viewer would hear: the default one if the file marks one.
    audio = next((s for s in audio_streams if s.get("disposition", {}).get("default")), None) \
        or (audio_streams[0] if audio_streams else {})
    duration = payload.get("format", {}).get("duration") or video.get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = None
    return {
        "duration": duration,
        "video_codec": (video.get("codec_name") or "").lower(),
        "pix_fmt": (video.get("pix_fmt") or "").lower(),
        "width": video.get("width"),
        "height": video.get("height"),
        "audio_codec": (audio.get("codec_name") or "").lower(),
        "audio_channels": audio.get("channels"),
        "audio_index": audio_streams.index(audio) if audio in audio_streams else 0,
        "audio_playable": (audio.get("codec_name") or "").lower() in PLAYABLE_AUDIO,
        "container": (payload.get("format", {}).get("format_name") or "").split(",")[0],
    }


def keyframes(path: str, start: float, end: float) -> list[float]:
    """Keyframe timestamps inside a window — the only honest cut points for a copy."""
    window_start = max(0.0, start - SIGNAL_WINDOW)
    window = end - window_start + SIGNAL_WINDOW
    # Read packet flags rather than decoded frames: no decoding, and it is the
    # only form that reports keyframes reliably for every container.
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-read_intervals", f"{window_start}%+{window}",
        "-of", "csv=p=0", path,
    ], timeout=180)
    if result.returncode != 0:
        return []
    times = []
    for line in result.stdout.splitlines():
        timestamp, _, flags = line.strip().partition(",")
        if "K" not in flags:
            continue
        try:
            times.append(float(timestamp))
        except ValueError:
            continue
    return sorted(times)


def keyframe_for_copy(path: str, start: float, tolerance: float = COPY_KEYFRAME_TOLERANCE) -> float | None:
    """The keyframe a stream copy could start from, if one is close enough.

    A copy cannot begin mid-GOP, and telling ffmpeg to start at a time between
    keyframes makes it silently include everything back to the previous one —
    a clip that starts seconds early. So the start is snapped to a keyframe
    when there is one nearby, and the caller is told where the clip really
    begins.
    """
    try:
        candidates = keyframes(path, max(0.0, start - tolerance), start + 1.0)
    except RenderError:
        return None
    before = [time for time in candidates if time <= start + 0.02]
    if not before:
        return None
    nearest = before[-1]
    return nearest if start - nearest <= tolerance else None


def silences(path: str, start: float, end: float) -> list[tuple[float, float]]:
    """Quiet stretches in a window, from ffmpeg's silencedetect filter."""
    window_start = max(0.0, start - SIGNAL_WINDOW)
    window = end - window_start + SIGNAL_WINDOW
    result = _run([
        "ffmpeg", "-v", "info", "-nostdin", "-ss", f"{window_start:.3f}", "-t", f"{window:.3f}",
        "-i", path, "-vn", "-af", f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MINIMUM}",
        "-f", "null", "-",
    ], timeout=300)
    spans: list[tuple[float, float]] = []
    open_start: float | None = None
    for line in (result.stderr or "").splitlines():
        found = re.search(r"silence_start: (-?[\d.]+)", line)
        if found:
            open_start = float(found.group(1)) + window_start
            continue
        found = re.search(r"silence_end: (-?[\d.]+)", line)
        if found:
            end_time = float(found.group(1)) + window_start
            if open_start is not None and end_time > open_start:
                spans.append((max(0.0, open_start), end_time))
            open_start = None
    if open_start is not None:
        spans.append((max(0.0, open_start), window_start + window))
    return spans


def scene_cuts(path: str, start: float, end: float) -> list[float]:
    """Timestamps where the picture changes shot, at thumbnail resolution.

    Decoding a 12-second window at 160px wide is cheap enough to do for every
    candidate, which is the whole point of doing it here rather than over the
    film.
    """
    window_start = max(0.0, start - SIGNAL_WINDOW)
    window = end - window_start + SIGNAL_WINDOW
    result = _run([
        "ffmpeg", "-v", "info", "-nostdin", "-ss", f"{window_start:.3f}", "-t", f"{window:.3f}",
        "-i", path, "-an", "-vf",
        f"scale=160:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo",
        "-f", "null", "-",
    ], timeout=300)
    cuts = []
    for line in (result.stderr or "").splitlines():
        if "pts_time:" not in line:
            continue
        found = re.search(r"pts_time:([\d.]+)", line)
        if found:
            cuts.append(float(found.group(1)) + window_start)
    return sorted(cuts)


@dataclass
class Signals:
    keyframes: list[float]
    silences: list[tuple[float, float]]
    cuts: list[float]


def gather_signals(path: str, start: float, end: float, *, on_note=None) -> Signals:
    """Every cheap signal around a candidate. Missing signals are not fatal."""
    def safe(function, *args):
        try:
            return function(*args)
        except RenderError as exc:
            if on_note:
                on_note(f"Media analysis was limited: {exc}")
            return []

    return Signals(
        keyframes=safe(keyframes, path, start, end),
        silences=safe(silences, path, start, end),
        cuts=safe(scene_cuts, path, start, end),
    )


# ── Boundary refinement ─────────────────────────────────────────────────────


def _nearest(values: list[float], target: float, tolerance: float) -> float | None:
    best, best_distance = None, tolerance
    for value in values:
        distance = abs(value - target)
        if distance <= best_distance:
            best, best_distance = value, distance
    return best


def _silence_edge(spans: list[tuple[float, float]], target: float, tolerance: float) -> float | None:
    """The middle of a quiet stretch near a boundary — the natural place to cut."""
    for span_start, span_end in spans:
        if span_start - tolerance <= target <= span_end + tolerance:
            return round((span_start + span_end) / 2, 3)
    return None


def refine_bounds(candidate: dict, segments: list, signals: Signals, *,
                  duration: float | None, min_seconds: float, max_seconds: float) -> dict:
    """Move a candidate's boundaries onto lines, cuts, quiet and keyframes.

    Returns the candidate with refined `start`/`end`, a `boundary_quality`
    between 0 and 1, and notes describing what each boundary landed on.
    """
    start = max(0.0, float(candidate["start"]))
    end = float(candidate["end"])
    if duration:
        end = min(end, duration)
    notes: list[str] = []
    quality = 0.0

    # 1. Talk to the subtitles: open on the line that starts the scene, close
    #    after the last line inside it has finished.
    opening = [s for s in segments if s.start <= start + 3.0 and s.end > start]
    if opening:
        line_start = opening[0].start
        # Step back to the beginning of the line being spoken — but only a
        # beat. A line that began long before the candidate is not a reason to
        # drag the whole clip backwards with it.
        if 0 <= start - (line_start - LEAD_IN) <= LINE_LOOKBACK:
            if start - line_start <= START_TOLERANCE:
                notes.append("opens on a line")
                quality += 0.3
            start = max(0.0, line_start - LEAD_IN)
    closing = [s for s in segments if s.end <= end + 2.0 and s.start >= start]
    if closing:
        aligned_end = closing[-1].end + TAIL
        if abs(aligned_end - end) <= END_TOLERANCE:
            notes.append("closes after a line")
            quality += 0.3
        end = max(end, aligned_end)

    # 2. A shot change is the cleanest possible start; a quiet moment is the
    #    cleanest possible end (nothing is said over the top of the cut). A
    #    forward snap is capped at the lead-in, so a line never loses its onset.
    cut = _nearest(signals.cuts, start, START_TOLERANCE)
    if cut is not None and cut - start <= LEAD_IN:
        start = cut
        notes.append("opens on a shot change")
        quality += 0.25
    else:
        quiet = _silence_edge(signals.silences, start, START_TOLERANCE)
        if quiet is not None:
            start = max(0.0, quiet)
            notes.append("opens in a quiet moment")
            quality += 0.15

    if not any("closes" in note for note in notes):
        quiet = _silence_edge(signals.silences, end, END_TOLERANCE)
        if quiet is not None:
            end = quiet + TAIL
            notes.append("closes in a quiet moment")
            quality += 0.15

    # 3. Length: prefer a subtitle boundary over a hard clamp, but never leave
    #    the configured range by much.
    span = end - start
    if span < min_seconds:
        # The first line that actually reaches the minimum, or the last one
        # inside the limit if none does, or the limit itself as a last resort.
        boundaries = [s.end + TAIL for s in segments
                      if s.end > end and s.end + TAIL - start <= max_seconds]
        reaching = [value for value in boundaries if value - start >= min_seconds]
        if reaching:
            end = reaching[0]
        elif boundaries:
            end = boundaries[-1]
        else:
            end = min(start + min_seconds, duration or start + min_seconds)
        notes.append("extended to reach the payoff")
    elif span > max_seconds:
        trimmed = [s.start for s in segments if start < s.start and s.start - start <= max_seconds]
        if trimmed:
            end = trimmed[-1]
            notes.append("trimmed to the last line inside the limit")
        else:
            end = start + max_seconds
            notes.append("trimmed to the length limit")

    if duration:
        end = min(end, duration)
    start = max(0.0, round(start, 3))
    end = round(max(end, start + 1.0), 3)

    refined = dict(candidate)
    refined.update({
        # The candidate's own times, kept so a later re-refinement can match
        # this scene back to the model's original answer (and its visual score).
        "raw_start": candidate["start"],
        "raw_end": candidate["end"],
        "start": start,
        "end": end,
        "duration": round(end - start, 3),
        "boundary_quality": round(min(1.0, quality), 3),
        "boundary_notes": notes,
    })
    return refined


# ── Cutting ─────────────────────────────────────────────────────────────────


def _ffmpeg_cut(source: str, output: Path, start: float, duration: float,
                profile: RenderProfile, mode: str, audio_index: int = 0) -> subprocess.CompletedProcess:
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-i", source, "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", f"0:a:{audio_index}?",
    ]
    if mode == "encode" or profile.force_encode:
        video_filter = profile.video_filter or "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        command += [
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
        ]
    else:
        command += ["-c:v", "copy"]
    if mode == "copy" and not profile.force_encode:
        command += ["-c:a", "copy"]
    else:
        command += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    command += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(output)]
    return _run(command, timeout=1800)


def verify_clip(path: Path, expected_duration: float, *,
                expect_audio: bool = False, playable_audio: bool = False) -> tuple[bool, str]:
    """A clip is only kept if it actually plays, sounds right, and is the right length."""
    if not path.exists() or path.stat().st_size < 20_000:
        return False, "the file was not written"
    info = probe(str(path))
    if not info.get("video_codec"):
        return False, "it has no picture"
    if expect_audio and not info.get("audio_codec"):
        return False, "it has no sound"
    if playable_audio and info.get("audio_codec") not in PLAYABLE_AUDIO:
        return False, f"its sound is {info.get('audio_codec')}, which browsers cannot play"
    actual = info.get("duration") or 0
    if expected_duration and abs(actual - expected_duration) > max(2.5, expected_duration * 0.15):
        return False, f"it is {actual:.1f}s long instead of {expected_duration:.1f}s"
    return True, ""


def cut_clip(source: str, output: Path, start: float, end: float,
             profile: RenderProfile = PROFILES[DEFAULT_PROFILE], *,
             has_audio: bool = True, audio_index: int = 0, audio_playable: bool = True,
             on_note=None) -> dict:
    """Write one clip from `start` to `end`, copying streams when that is exact.

    Copying is preferred because it is lossless and near-instant. It can only
    begin on a keyframe, though, so a copy is only attempted when one sits
    within `COPY_KEYFRAME_TOLERANCE` of the requested start; otherwise the clip
    is encoded, which cuts exactly where it was asked to. The returned
    `actual_start` is where the clip really begins — a copy can sit up to that
    tolerance earlier — so callers can time subtitles against it.
    """
    keyframe = None if profile.force_encode else keyframe_for_copy(source, start)
    copy_start = keyframe if keyframe is not None else start
    copy_duration = max(0.5, end - copy_start)

    # A copy is only worth trying when it would carry sound the browser can
    # play; otherwise the video is still copied but the audio is re-encoded.
    copy_sound = has_audio and audio_playable
    modes = ["encode"] if profile.force_encode or keyframe is None else (
        ["copy", "audio", "encode"] if copy_sound else ["audio", "encode"]
    )
    failures: list[str] = []
    for mode in modes:
        if mode == "encode":
            clip_start, clip_duration = start, max(0.5, end - start)
        else:
            clip_start, clip_duration = copy_start, copy_duration
        result = _ffmpeg_cut(source, output, clip_start, clip_duration, profile, mode, audio_index)
        if result.returncode != 0:
            failures.append(f"{mode}: {(result.stderr or '').strip().splitlines()[-1:] or ['failed']}")
            continue
        ok, reason = verify_clip(output, clip_duration, expect_audio=has_audio,
                                 playable_audio=has_audio)
        if ok:
            return {
                "mode": mode,
                "size": output.stat().st_size,
                "actual_start": round(clip_start, 3),
                "duration": round(clip_duration, 3),
            }
        failures.append(f"{mode}: {reason}")
        if on_note:
            on_note(f"Retrying clip {output.name} ({reason}).")
    raise RenderError(f"The clip could not be produced ({'; '.join(failures)}).")


def make_thumbnail(source: str, output: Path, at: float, *, width: int = 640) -> bool:
    result = _run([
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{max(0.0, at):.3f}",
        "-i", source, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(output),
    ], timeout=300)
    return result.returncode == 0 and output.exists()


def build_contact_sheet(source: str, output: Path, start: float, duration: float,
                        *, columns: int = 3, width: int = 320) -> bool:
    """Three frames from a clip, side by side, for the vision pass.

    One image per scene keeps the vision call cheap: nine separate images would
    cost nine times the tokens for the same judgement.
    """
    offsets = [duration * fraction for fraction in (0.08, 0.5, 0.92)][:columns]
    # `tile` needs its inputs in one filter graph; `select` by frame time is the
    # reliable way to sample three points in a single decode.
    expression = "+".join(f"eq(n\\,{int(round(offset * 25))})" for offset in offsets)
    result = _run([
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{max(0.0, start):.3f}",
        "-i", source, "-t", f"{max(1.0, duration):.3f}",
        "-vf", f"fps=25,select='{expression}',scale={width}:-2,tile={columns}x1",
        "-frames:v", "1", "-q:v", "5", str(output),
    ], timeout=600)
    return result.returncode == 0 and output.exists()


def write_srt(segments: list[dict], output: Path) -> bool:
    """A sidecar SubRip file beside the clip, timed to the clip's own start."""
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{_srt_time(segment['start'])} --> {_srt_time(segment['end'])}")
        lines.append(segment["text"])
        lines.append("")
    try:
        output.write_text("\n".join(lines), encoding="utf-8")
        return True
    except OSError:
        return False


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
