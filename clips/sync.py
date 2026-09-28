"""Checking that subtitles actually line up with the film.

A subtitle file is only useful if its timings match the release it is played
against, and files found online frequently do not: they were timed for a
different cut, or for a 25 fps broadcast of a 23.976 fps film, or simply carry
a constant offset. Since the clip pipeline takes its scene boundaries from
subtitle timings, believing an out-of-sync file would put every clip in the
wrong place — and the subtitles saved beside the film would be wrong for
playback too.

The measurement is local and cheap. A few short windows are sampled across the
film, and in each one the speech is located with a voice-activity detector
(a level threshold is not enough: a music score reads as "speech" to one, which
is how a film like Inception defeats it). The subtitles are then slid against
that speech to find the offset where they line up best.

If every window wants the same offset, the file is simply shifted, and the
shift is applied. If instead the offset grows steadily across the film, the
file was timed for a different frame rate — those ratios are known, so each is
tried and the one that makes the windows agree is applied. If neither holds,
the file is for a different release and is rejected rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .transcript import Segment

# Sampling: eight short windows spread across the film. Enough of them that a
# film with quiet stretches still yields two windows that can carry a verdict,
# and short enough that decoding them all costs a few seconds.
SAMPLE_COUNT = 8
SAMPLE_SECONDS = 90.0
EDGE_MARGIN = 0.06

SAMPLE_RATE = 16_000
FRAME = 0.02                      # alignment resolution: 20 ms
# A subtitle start is expected within this much of a speech start. Cue text
# leads the voice slightly, and the detector has its own latency.
LEAD_TOLERANCE = 0.5
# How far a subtitle may be shifted and still be considered the same film's.
# A constant offset comes from a different intro; anything beyond this is a
# different cut, and correcting it would be guesswork.
SEARCH_SECONDS = 30.0

# Frame-rate mismatches worth trying: film (23.976 and 24) against PAL (25),
# and against each other. A PAL speedup of a film print is 25/24, and a film
# print played at 25 fps is 25/23.976 — the two common mistakes.
FPS_RATIOS: tuple[tuple[float, str], ...] = (
    (1.0, ""),
    (25 / 23.976, "25 vs 23.976 fps"),
    (23.976 / 25, "23.976 vs 25 fps"),
    (25 / 24, "25 vs 24 fps"),
    (24 / 25, "24 vs 25 fps"),
    (24 / 23.976, "24 vs 23.976 fps"),
    (23.976 / 24, "23.976 vs 24 fps"),
)

# A shift below this is left alone: it is inside the noise of the measurement,
# and moving subtitles by less than a second risks making a good file worse.
NEGLIGIBLE_OFFSET = 1.0
# How far apart the windows may be before the median stops meaning anything.
# Loose on purpose: individual windows disagree for innocent reasons (a stretch
# of music, captioned sounds, the quieter end of a part-covered file) and the
# consensus throws those windows out anyway. What it must not do is let a file
# through that no window can read: those form no consensus at all.
AGREEMENT = 8.0
# A rival frame rate must explain the film markedly better to be believed.
RATIO_IMPROVEMENT = 0.5
# A window needs this many subtitles, and this share of their starts landing on
# speech, before it is allowed to vote. Measured against a real film: a matching
# file scores about 0.80 there, subtitles for another film 0.43 or less, and a
# file timed for the wrong frame rate sits between the two.
MIN_CUES = 12
MIN_CUES_PER_WINDOW = 8
# A window is only believed if this share of its subtitles land on speech at
# the shift the windows agree on...
MIN_WINDOW_MATCH = 0.3
# ...and the file is only corrected if, pooled over those windows, this share
# do. Measured on a real film: a matching file scores 0.5 and up, subtitles for
# another release 0.25 or less, and a file timed for the wrong frame rate sits
# between unless the ratio is corrected first.
MIN_QUALITY = 0.45
# A frame rate that is not the film's own must beat it by this much to be
# adopted, so a marginal improvement is not mistaken for a real mismatch.
GOOD_MATCH = 0.55
RATIO_MARGIN = 0.06
# Measured across two films: a subtitle that matches agrees in 5-8 windows,
# while unrelated cues never agree in more than four, however dense they are.
MIN_WINDOWS_AGREEING = 5
MIN_SPEECH_SECONDS = 8.0
MIN_SPEECH_RATIO = 0.12

OK = "ok"
CORRECTED = "corrected"
UNUSABLE = "unusable"
UNKNOWN = "unknown"


@dataclass
class SyncResult:
    status: str
    offset: float = 0.0      # seconds to add to subtitle times
    scale: float = 1.0       # multiplier applied to subtitle times
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        return self.status in (OK, CORRECTED)

    def describe(self) -> str:
        if self.status == OK:
            return "in sync"
        if self.status == CORRECTED:
            parts = []
            if abs(self.scale - 1.0) > 1e-6:
                parts.append(f"rescaled {self.scale:.4f}x")
            if abs(self.offset) > 0.05:
                parts.append(f"shifted {self.offset:+.1f}s")
            return "corrected (" + ", ".join(parts or ["aligned"]) + ")"
        if self.status == UNUSABLE:
            return "not in sync with this release"
        return "sync could not be measured"


# ── Speech, and where the subtitles sit against it ──────────────────────────


def _pcm(source_path: str, start: float, duration: float):
    """Mono 16 kHz audio for one window, straight out of the film."""
    import numpy as np
    import subprocess

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, start):.3f}",
         "-t", f"{duration:.3f}", "-i", source_path, "-vn", "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        capture_output=True, timeout=600, check=False,
    ).stdout
    if len(raw) < SAMPLE_RATE:  # under half a second of audio: nothing to measure
        return None
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0


def _speech_intervals(audio, start: float) -> list[tuple[float, float]]:
    """Where somebody is talking, according to a voice-activity detector."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(threshold=0.5, min_speech_duration_ms=180,
                         min_silence_duration_ms=250, speech_pad_ms=40)
    stamps = get_speech_timestamps(audio, options, sampling_rate=SAMPLE_RATE)
    return [(stamp["start"] / SAMPLE_RATE + start, stamp["end"] / SAMPLE_RATE + start)
            for stamp in stamps]


def _start_train(times, start: float, frames: int):
    """One frame marked at each of these times."""
    import numpy as np

    train = np.zeros(frames, dtype=np.float32)
    for time in times:
        index = int(round((time - start) / FRAME))
        if 0 <= index < frames:
            train[index] = 1.0
    return train


def _lead_window(times, start: float, frames: int):
    """Marked wherever one of these times falls, give or take the lead."""
    import numpy as np

    span = int(LEAD_TOLERANCE / FRAME)
    kernel = np.ones(2 * span + 1, dtype=np.float32)
    return np.clip(np.convolve(_start_train(times, start, frames), kernel, "same"), 0.0, 1.0)


def _windows(first: float, last: float, count: int, length: float) -> list[float]:
    """Evenly spaced window starts across the stretch the subtitles cover."""
    if last <= first:
        return []
    usable = last - first
    length = min(length, max(30.0, usable / max(1, count)))
    return [first + usable * (index + 0.5) / count - length / 2
            for index in range(count)]


@dataclass
class _Window:
    start: float
    speech: list[tuple[float, float]]

    @property
    def speech_seconds(self) -> float:
        return sum(end - start for start, end in self.speech)


def _sample_windows(source_path: str, segments: list[Segment], duration: float,
                    *, on_note=None) -> tuple[list[_Window], SyncResult | None]:
    """The windows worth measuring, with the speech found in each."""
    first = min(segment.start for segment in segments)
    last = max(segment.end for segment in segments)
    # Stay inside the film, and inside the subtitles where they do not cover it
    # all: a file for the first half can only be judged against the first half.
    span_start = max(0.0, first - SAMPLE_SECONDS / 2)
    span_end = min(duration, last + SAMPLE_SECONDS / 2)
    windows: list[_Window] = []
    for start in _windows(span_start, span_end, SAMPLE_COUNT, SAMPLE_SECONDS):
        end = start + SAMPLE_SECONDS
        if end <= 0 or start >= duration:
            continue
        audio = _pcm(source_path, start, SAMPLE_SECONDS)
        if audio is None:
            continue
        try:
            speech = _speech_intervals(audio, start)
        except ImportError:
            return [], SyncResult(
                UNKNOWN,
                notes=["install faster-whisper to check subtitle sync against the film"],
            )
        except Exception as exc:  # a broken VAD must not fail the job
            if on_note:
                on_note(f"Could not measure subtitle sync: {type(exc).__name__}: {exc}")
            return [], SyncResult(UNKNOWN, notes=["speech detection failed"])
        window = _Window(start, speech)
        if window.speech_seconds < max(MIN_SPEECH_SECONDS, MIN_SPEECH_RATIO * SAMPLE_SECONDS):
            continue
        windows.append(window)
    return windows, None


@dataclass
class _WindowCurve:
    """One window's opinion: how well the subtitles match speech at each shift."""

    start: float
    offsets: "object"      # numpy array of candidate shifts
    scores: "object"       # numpy array, matches at each shift (counts)
    votes: float           # subtitle starts this window could judge by
    @property
    def best(self) -> float:
        import numpy as np
        return float(self.offsets[int(np.argmax(self.scores))])

    def at(self, offset: float) -> float:
        """The share of this window's subtitles that match at a given shift."""
        import numpy as np
        return float(self.scores[int(np.argmin(np.abs(self.offsets - offset)))]) / self.votes


def _curves(windows: list[_Window], cues: list[tuple[float, float]],
            ratio: float) -> list[_WindowCurve]:
    """Score every shift, window by window, at one frame rate."""
    import numpy as np

    scaled = [(start * ratio, end * ratio) for start, end in cues]
    curves: list[_WindowCurve] = []
    for window in windows:
        frames = int(SAMPLE_SECONDS / FRAME)
        speech_leads = _lead_window([start for start, _ in window.speech], window.start, frames)
        cue_starts = _start_train([start for start, _ in scaled], window.start, frames)
        votes = float(cue_starts.sum())
        if votes < MIN_CUES_PER_WINDOW:
            continue
        correlation = np.correlate(cue_starts, speech_leads, mode="full")
        # `correlate` runs its lag axis from -(len(speech)-1) to len(cues)-1,
        # and the sign that falls out *undoes* the misalignment: matches are
        # found by moving the subtitles, so a file that runs late scores
        # negative here.
        lags = np.arange(len(speech_leads) - 1, -len(cue_starts), -1)
        inside = np.abs(lags * FRAME) <= SEARCH_SECONDS
        curves.append(_WindowCurve(window.start, lags[inside] * FRAME, correlation[inside], votes))
    return curves


def _pooled(curves: list[_WindowCurve]) -> dict | None:
    """Every window's evidence summed at each shift.

    Weaker than the consensus below at placing an offset precisely — one
    unreadable window drags the peak — but it uses every window, which is what
    makes it the right question to ask when *choosing a frame rate*: at the
    wrong one, no single shift fits the film, and that shows up here as a low
    peak no matter how the windows are weighted.
    """
    import numpy as np

    if not curves:
        return None
    pooled = np.zeros(len(curves[0].offsets))
    votes = 0.0
    for curve in curves:
        pooled += curve.scores
        votes += curve.votes
    if votes <= 0:
        return None
    scores = pooled / votes
    best = int(np.argmax(scores))
    return {"offset": float(curves[0].offsets[best]), "quality": float(scores[best])}


def _consensus(curves: list[_WindowCurve]) -> dict | None:
    """The shift the windows agree on, and how well they agree.

    The median of the windows' own best shifts is the robust part: a window
    over a stretch the detector could not read well — a music cue, a whisper —
    can peak anywhere, and a mean would follow it. Only windows that score
    reasonably *at that consensus* are then allowed to set the final number.
    """
    import numpy as np

    if len(curves) < 2:
        return None
    consensus = float(np.median([curve.best for curve in curves]))
    keep = [curve for curve in curves if curve.at(consensus) >= MIN_WINDOW_MATCH]
    # Three windows, not two: a rescale that is wrong by a fraction of a per
    # cent can line two windows up by luck, and two votes are not enough to
    # tell that from a file that genuinely matches the film.
    if len(keep) < MIN_WINDOWS_AGREEING:
        return None
    offsets = keep[0].offsets
    pooled = np.zeros(len(offsets))
    votes = 0.0
    for curve in keep:
        pooled += curve.scores
        votes += curve.votes
    scores = pooled / votes
    best = int(np.argmax(scores))
    shifts = [curve.at(float(offsets[best])) for curve in keep]
    return {
        "offset": float(offsets[best]),
        "quality": float(scores[best]),
        "spread": max(curve.best for curve in keep) - min(curve.best for curve in keep),
        "windows": len(keep),
        "readings": shifts,
    }


def verify(source_path: str, segments: list[Segment], duration: float | None, *,
           on_note=None) -> SyncResult:
    """Measure whether a transcript lines up with the film, and how."""
    if not duration or duration < 300:
        return SyncResult(UNKNOWN, notes=["too short to measure"])
    if len(segments) < MIN_CUES:
        return SyncResult(UNKNOWN, notes=[f"only {len(segments)} cues to align against"])

    windows, failure = _sample_windows(source_path, segments, duration, on_note=on_note)
    if failure:
        return failure
    if len(windows) < 2:
        return SyncResult(UNKNOWN, notes=["not enough speech to measure sync"])

    cues = [(segment.start, segment.end) for segment in segments]

    def rate(ratio: float, label: str) -> dict | None:
        """How well this frame rate explains the film, and where it lines up."""
        curves = _curves(windows, cues, ratio)
        pooled = _pooled(curves)
        if pooled is None:
            return None
        return {"ratio": ratio, "label": label, "curves": curves, "pooled": pooled}

    # The film's own frame rate is assumed first: most subtitle files are timed
    # for the right one, and a rival has to explain the film clearly better —
    # not merely marginally — before it is believed.
    rated = [rate(1.0, "")]
    if rated[0] is None or rated[0]["pooled"]["quality"] < GOOD_MATCH:
        rated.extend(rate(ratio, label) for ratio, label in FPS_RATIOS[1:])
    rated = [entry for entry in rated if entry]
    if not rated:
        return SyncResult(UNKNOWN, notes=["not enough speech to measure sync"])

    best = max(rated, key=lambda entry: entry["pooled"]["quality"])
    if best["ratio"] != 1.0 and rated[0]["ratio"] == 1.0:
        margin = best["pooled"]["quality"] - rated[0]["pooled"]["quality"]
        if margin < RATIO_MARGIN:
            best = rated[0]

    # The verdict comes from the windows' consensus, never from the pooled
    # curve: pooling every window is what makes a frame rate identifiable, but
    # it is also willing to believe a file that only nearly fits — measured at
    # 0.62 for subtitles belonging to a different film. The consensus throws
    # unreadable windows out instead, and when too few windows survive there is
    # no verdict to give.
    agreed = _consensus(best["curves"])
    if not agreed or agreed["quality"] < MIN_QUALITY or agreed["spread"] > AGREEMENT:
        # Windows full of speech, and the subtitles line up with none of it, or
        # with too little of it to be sure. Either way they are not usable.
        return SyncResult(UNUSABLE, notes=["subtitle timing does not match this release"])

    offset = round(agreed["offset"], 3)
    quality = agreed["quality"]
    notes = [f"{agreed['windows']} windows agreed out of {len(windows)}"]
    if best["label"]:
        notes.append(f"timed for {best['label']}")
    if best["ratio"] == 1.0 and abs(offset) <= NEGLIGIBLE_OFFSET:
        return SyncResult(OK, confidence=round(quality, 3),
                          notes=notes + [f"aligned within {abs(offset):.2f}s"])
    return SyncResult(CORRECTED, offset=offset, scale=best["ratio"],
                      confidence=round(quality, 3),
                      notes=notes + ([f"every sample was {offset:+.2f}s out"]
                                     if abs(offset) > 0.05 else []))


def corrected(segments: list[Segment], result: SyncResult, duration: float | None) -> list[Segment]:
    """A transcript re-timed to match the film."""
    if result.status != CORRECTED:
        return segments
    fixed: list[Segment] = []
    for segment in segments:
        start = segment.start * result.scale + result.offset
        end = segment.end * result.scale + result.offset
        if duration:
            if start >= duration:
                continue
            end = min(end, duration)
        if end - start <= 0.05:
            continue
        fixed.append(Segment(max(0.0, start), end, segment.text))
    return fixed
