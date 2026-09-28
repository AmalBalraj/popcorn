"""Turning a movie into a timestamped transcript.

Subtitles are the cheapest source of "what is said, and when", so they are
tried in order of cost before anything is transcribed:

1. a sidecar file next to the movie (`.srt`, `.vtt`, `.ass`, `.ssa`) — free,
   a local read through the existing library mount;
2. a text subtitle track Jellyfin already knows about, including subtitles
   embedded in the container — one HTTP request, no decoding here;
3. text subtitle tracks read straight out of the container with ffmpeg;
4. Whisper, which is the only step that has to listen to the whole film.

Every path ends in the same `Transcript`, so nothing downstream needs to care
where the words came from.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# A subtitle file that covers less than this much speech, or fewer cues than
# this, is almost always a "forced" track holding only the foreign-language
# lines. Analysing one would produce clips chosen from a handful of fragments.
MIN_CUES = 40
MIN_SPEECH_RATIO = 0.03

SIDECAR_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")
# Subtitle codecs that carry pictures rather than text. They cannot be read
# without OCR, so they are skipped rather than sent to ffmpeg to fail.
BITMAP_SUBTITLE_CODECS = {
    "dvd_subtitle", "dvb_subtitle", "hdmv_pgs_subtitle", "pgssub",
    "xsub", "dvb_teletext",
}

_LANGUAGE_NAMES = {
    "en": "en", "eng": "en", "english": "en",
    "ml": "ml", "mal": "ml", "malayalam": "ml",
    "ta": "ta", "tam": "ta", "tamil": "ta",
    "te": "te", "tel": "te", "telugu": "te",
    "hi": "hi", "hin": "hi", "hindi": "hi",
    "kn": "kn", "kan": "kn", "kannada": "kn",
    "bn": "bn", "ben": "bn", "bengali": "bn",
    "mr": "mr", "mar": "mr", "marathi": "mr",
    "pa": "pa", "pan": "pa", "punjabi": "pa",
    "es": "es", "spa": "es", "spanish": "es",
    "fr": "fr", "fre": "fr", "fra": "fr", "french": "fr",
    "de": "de", "ger": "de", "deu": "de", "german": "de",
    "ja": "ja", "jpn": "ja", "japanese": "ja",
    "ko": "ko", "kor": "ko", "korean": "ko",
    "zh": "zh", "zho": "zh", "chi": "zh", "chinese": "zh",
    "ar": "ar", "ara": "ar", "arabic": "ar",
    "ru": "ru", "rus": "ru", "russian": "ru",
    "pt": "pt", "por": "pt", "portuguese": "pt",
    "it": "it", "ita": "it", "italian": "it",
    "nl": "nl", "dut": "nl", "nld": "nl", "dutch": "nl",
    "tr": "tr", "tur": "tr", "turkish": "tr",
}

_TAG = re.compile(r"<[^>]+>")
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")
_ASS_DRAWING = re.compile(r"^m\s|^p\s*\d", re.IGNORECASE)
_ARROWS = re.compile(r"-{1,3}>")
# Release-group watermarks. They are cues like any other, and left in they can
# be picked as "iconic dialogue" ("Downloaded From www.AllSubs.org").
_NOISE = re.compile(
    r"(downloaded\s+from|subtitles?\s+(by|from)|synced?\s+(and\s+corrected\s+)?by|"
    r"corrected\s+by|opensubtitles|subscene|addic7ed|yify|yts\.|www\.\S+\.(?:com|org|net|in|mx|to|bz|cc|me)|"
    r"advertise\s+your|support\s+us|donate|torrent)",
    re.IGNORECASE,
)
_NOISE_MAX_LENGTH = 90


class TranscriptionUnavailable(Exception):
    """Speech-to-text is not usable on this server."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


@dataclass
class Transcript:
    segments: list[Segment]
    source: str          # sidecar | jellyfin | embedded | downloaded | whisper
    detail: str          # what exactly was read, for the interface to name
    language: str | None = None
    corrected: bool = False   # timings were shifted or rescaled to match the film

    @property
    def digest(self) -> str:
        """Fingerprint of the words and their timing, for the analysis cache."""
        hasher = hashlib.sha256()
        for segment in self.segments:
            hasher.update(f"{segment.start:.3f}|{segment.end:.3f}|{segment.text}\n".encode("utf-8"))
        return hasher.hexdigest()[:32]

    def as_list(self) -> list[dict]:
        return [segment.as_dict() for segment in self.segments]

    def window(self, start: float, end: float) -> list[Segment]:
        """Segments overlapping a time range, keeping their absolute times."""
        return [s for s in self.segments if s.end > start and s.start < end]

    def rebased(self, start: float, end: float) -> list[dict]:
        """Segments inside a clip, re-timed to the clip's own timeline.

        These are what a future captioned render burns in, so they are stored
        with the clip rather than recomputed.
        """
        rebased = []
        for segment in self.window(start, end):
            clipped_start = max(segment.start, start) - start
            clipped_end = min(segment.end, end) - start
            if clipped_end - clipped_start < 0.08:
                continue
            rebased.append({
                "start": round(clipped_start, 3),
                "end": round(clipped_end, 3),
                "text": segment.text,
            })
        return rebased


def usable(transcript: Transcript, duration: float | None) -> tuple[bool, str]:
    """Whether a subtitle track is worth analysing.

    Guards against forced/foreign-only tracks and against stub files that
    contain a handful of cues.
    """
    if not transcript.segments:
        return False, "no cues"
    if len(transcript.segments) < MIN_CUES:
        return False, f"only {len(transcript.segments)} cues"
    speech = sum(max(0.0, s.end - s.start) for s in transcript.segments)
    if duration and speech < MIN_SPEECH_RATIO * duration:
        return False, f"only {speech:.0f}s of speech in {duration:.0f}s"
    if duration and transcript.segments[-1].end < 0.4 * duration:
        return False, "ends before the film does"
    return True, ""


# ── Parsing ─────────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    text = _ASS_OVERRIDE.sub("", text)
    text = text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    text = _TAG.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def _is_noise(text: str) -> bool:
    """Group watermarks and "subtitles by" credits, not lines of the film."""
    return len(text) <= _NOISE_MAX_LENGTH and bool(_NOISE.search(text))


def _seconds(value: str) -> float | None:
    value = value.strip().replace(",", ".")
    match = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$", value)
    if not match:
        return None
    hours, minutes, secs, fraction = match.groups()
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
    if fraction:
        total += float(f"0.{fraction}")
    return total


def parse_srt(text: str) -> list[Segment]:
    """SubRip, and the WebVTT that mostly looks like it."""
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_index = 0
        if _ARROWS.search(lines[0]) is None:
            timing_index = 1
            if len(lines) < 2 or _ARROWS.search(lines[1]) is None:
                continue
        parts = _ARROWS.split(lines[timing_index])
        if len(parts) < 2:
            continue
        start = _seconds(parts[0])
        end = _seconds(parts[1].split()[0] if parts[1].split() else "")
        if start is None or end is None or end <= start:
            continue
        body = _clean(" ".join(lines[timing_index + 1:]))
        if body and not _is_noise(body):
            segments.append(Segment(start, end, body))
    return segments


def parse_vtt(text: str) -> list[Segment]:
    body = re.sub(r"^WEBVTT.*?(?:\n\s*\n)", "", text, count=1, flags=re.DOTALL)
    body = re.sub(r"^(?:NOTE|STYLE|REGION)\b.*?(?:\n\s*\n)", "", body, flags=re.DOTALL | re.MULTILINE)
    return parse_srt(body)


def parse_ass(text: str) -> list[Segment]:
    """Advanced SubStation Alpha.

    Only `Dialogue:` lines carry speech; `Comment:` lines are notes to the
    renderer and would otherwise become transcript text.
    """
    segments: list[Segment] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.lower().startswith("dialogue:"):
            continue
        fields = line.split(":", 1)[1].split(",", 9)
        if len(fields) < 10:
            continue
        start, end, body = _seconds(fields[1]), _seconds(fields[2]), fields[9]
        if start is None or end is None or end <= start:
            continue
        if _ASS_DRAWING.match(body.strip()):
            continue
        cleaned = _clean(body)
        if cleaned and not _is_noise(cleaned):
            segments.append(Segment(start, end, cleaned))
    return segments


def parse_subtitle(text: str) -> list[Segment]:
    """Parse whichever subtitle format this text is, by inspection."""
    head = text.lstrip()[:400].upper()
    if head.startswith("WEBVTT"):
        return parse_vtt(text)
    if "[SCRIPT INFO]" in head or "DIALOGUE:" in head:
        return parse_ass(text)
    if _ARROWS.search(text[:4000]):
        return parse_srt(text)
    if "[EVENTS]" in head:
        return parse_ass(text)
    return []


# ── Finding subtitles ───────────────────────────────────────────────────────


def _language_tag(stem: str) -> str | None:
    """The language a subtitle filename advertises, if it advertises one."""
    for token in reversed(re.split(r"[.\-_ \[\]()]+", stem.lower())):
        if token in _LANGUAGE_NAMES:
            return _LANGUAGE_NAMES[token]
    return None


def _sidecar_score(path: Path, video: Path, preferred: str) -> int:
    name = path.stem.lower()
    score = {".srt": 4, ".vtt": 3, ".ass": 2, ".ssa": 1}.get(path.suffix.lower(), 0)
    if video.stem.lower() in name or name in video.stem.lower():
        score += 6
    language = _language_tag(path.stem)
    if language == preferred:
        score += 5
    elif language == "en":
        score += 2
    elif language is None:
        score += 1
    if "forced" in name:
        score -= 8
    if "sdh" in name or "hearing" in name or re.search(r"\bhi\b", name):
        score -= 1
    if "commentary" in name:
        score -= 8
    return score


def find_sidecar(video: Path, preferred_language: str, *, log=None) -> Transcript | None:
    """The best readable subtitle file sitting next to the movie."""
    try:
        candidates = [
            entry for entry in video.parent.iterdir()
            if entry.is_file() and entry.suffix.lower() in SIDECAR_EXTENSIONS
        ]
    except OSError:
        return None
    candidates.sort(key=lambda path: _sidecar_score(path, video, preferred_language), reverse=True)

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        segments = parse_subtitle(text)
        if not segments:
            continue
        transcript = Transcript(segments, "sidecar", path.name, _language_tag(path.stem))
        ok, reason = usable(transcript, None)
        if ok:
            return transcript
        if log:
            log(f"Skipping {path.name}: {reason}.")
    return None


def probe_text_subtitle_streams(path: str) -> list[dict]:
    """Text subtitle tracks inside the container, in stream order."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s", "-show_streams",
             "-of", "json", path],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    try:
        streams = json.loads(result.stdout or "{}").get("streams", [])
    except ValueError:
        return []

    tracks = []
    for position, stream in enumerate(streams):
        codec = (stream.get("codec_name") or "").lower()
        if codec in BITMAP_SUBTITLE_CODECS:
            continue
        tracks.append({
            "position": position,
            "codec": codec,
            "language": _LANGUAGE_NAMES.get(
                (stream.get("tags", {}).get("language") or "").lower(),
                (stream.get("tags", {}).get("language") or "").lower() or None,
            ),
            "title": stream.get("tags", {}).get("title"),
        })
    return tracks


def extract_embedded(path: str, position: int) -> list[Segment]:
    """Read one embedded text track out of the container as SubRip."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", path,
             "-map", f"0:s:{position}", "-f", "srt", "-"],
            capture_output=True, text=True, timeout=600, check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return parse_srt(result.stdout)


# ── Speech-to-text ──────────────────────────────────────────────────────────

WHISPER_SAMPLE_RATE = 16000


def extract_audio(source_path: str, work_dir: Path, *, spawn, duration: float | None,
                  on_progress=None) -> Path:
    """Pull a 16 kHz mono track out of the movie for Whisper to listen to."""
    work_dir.mkdir(parents=True, exist_ok=True)
    audio_path = work_dir / "audio.wav"
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", source_path,
        "-vn", "-ac", "1", "-ar", str(WHISPER_SAMPLE_RATE), "-c:a", "pcm_s16le",
        "-progress", "pipe:1", "-nostats", str(audio_path),
    ]
    process = spawn(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        if on_progress and duration and line.startswith("out_time_ms="):
            try:
                seconds = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            on_progress(min(seconds, duration), duration, "Extracting the soundtrack")
    return_code = process.wait()
    if return_code != 0 or not audio_path.exists():
        lines = [line.strip() for line in (process.stderr.read() if process.stderr else "").splitlines() if line.strip()]
        # Prefer a line that names the cause, and never quote our own paths back
        # at whoever is reading the job.
        detail = next((line for line in reversed(lines) if "error" in line.lower()), lines[-1] if lines else "")
        detail = detail.replace(str(work_dir), "").strip(" :")
        raise RuntimeError(
            "The soundtrack could not be extracted from this movie"
            + (f": {detail}" if detail else ".")
        )
    return audio_path


def transcribe_with_whisper(
    audio_path: Path, *, model_size: str, cpu_threads: int, duration: float | None,
    language: str | None = None, on_progress=None, on_note=None,
) -> Transcript:
    """Speech-to-text with faster-whisper, running in this process.

    Only reached when a movie has no usable subtitles at all: it is the one
    stage that must listen to every second of the film.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - depends on the server
        raise TranscriptionUnavailable(
            "Speech-to-text is not installed on this server. Install it with "
            "“python3 -m pip install faster-whisper”, or add subtitles for this movie."
        ) from exc

    if on_note:
        on_note(f"Loading the “{model_size}” speech model (downloaded once)…")
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8",
                             cpu_threads=max(1, cpu_threads))
    except Exception as exc:  # model download, disk, or backend failure
        raise TranscriptionUnavailable(
            f"The speech model could not be loaded: {type(exc).__name__}: {exc}"
        ) from exc

    segments: list[Segment] = []
    try:
        stream, _info = model.transcribe(
            str(audio_path),
            language=language or None,
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
        )
        for piece in stream:
            text = _clean(piece.text or "")
            if text:
                segments.append(Segment(float(piece.start), float(piece.end), text))
            if on_progress and duration:
                on_progress(min(float(piece.end), duration), duration, "Transcribing the film")
    except Exception as exc:  # decoding or backend failure mid-run
        raise TranscriptionUnavailable(
            f"Transcription failed: {type(exc).__name__}: {exc}"
        ) from exc

    if not segments:
        raise TranscriptionUnavailable("No speech was recognised in this movie.")
    return Transcript(segments, "whisper", f"Whisper ({model_size})", language or None)
