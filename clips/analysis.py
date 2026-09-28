"""Asking a model which moments are worth clipping, and ranking the answers.

Everything here is text-first. The transcript is chunked with overlap so no
scene is cut in half at a chunk boundary, each chunk is sent with a forced
tool call so the reply is structured data rather than prose, and every field is
validated and clamped before it is allowed anywhere near ffmpeg.

The ranking is deterministic Python, not model judgement: the model proposes,
the score disposes.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import requests

# Model judgement is only as good as the excerpt it sees, so chunks overlap by
# roughly a minute of dialogue: a scene that starts near the end of one chunk
# is still complete in the next. Larger chunks mean fewer calls and less
# duplicated overlap, at the cost of a longer excerpt for the model to weigh.
CHUNK_CHARACTERS = 16000
CHUNK_OVERLAP_SEGMENTS = 8
MAX_CANDIDATES_PER_CHUNK = 6
# Scenes shorter than this are never worth a clip, whatever the model says.
MIN_CANDIDATE_SECONDS = 12.0

CATEGORIES = {
    "tension", "humour", "iconic", "conflict", "reveal", "twist", "suspense",
    "inspirational", "romance", "action", "emotional", "other",
}

# How much each category is worth beyond what the model scored it. A twist or
# a reveal is what makes a clip travel; exposition is what makes it die.
CATEGORY_WEIGHT = {
    "twist": 1.0, "reveal": 1.0, "iconic": 0.95, "emotional": 0.9,
    "tension": 0.9, "conflict": 0.85, "inspirational": 0.85, "humour": 0.85,
    "suspense": 0.8, "romance": 0.8, "action": 0.75, "other": 0.5,
}

# Openings and endings are titles, credits and establishing shots: a clip
# chosen there is almost always wrong, so candidates are penalised inside
# these windows rather than rejected outright.
EDGE_WINDOW = 0.04
MAX_OVERLAP = 0.35

SCORE_WEIGHTS = {
    "interest": 0.46,       # the model's own judgement of the moment
    "dialogue": 0.14,       # quotability
    "self_contained": 0.14, # makes sense without the rest of the film
    "category": 0.10,       # what kind of moment it is
    "boundary": 0.11,       # how cleanly the clip starts and ends
    "visual": 0.05,         # only when the vision pass ran
}

_CANDIDATE_TOOL = {
    "name": "record_clip_candidates",
    "description": "Record the scenes from this excerpt that would make good short-form clips.",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number", "description": "Start time in seconds from the beginning of the film."},
                        "end": {"type": "number", "description": "End time in seconds from the beginning of the film."},
                        "title": {"type": "string", "description": "Short title of the moment, at most 8 words."},
                        "summary": {"type": "string", "description": "One sentence describing what happens."},
                        "category": {"type": "string", "enum": sorted(CATEGORIES)},
                        "hook": {"type": "string", "description": "The line or beat that pulls a viewer in."},
                        "interest_score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "context_score": {"type": "integer", "minimum": 0, "maximum": 100,
                                          "description": "How well the clip stands alone without the rest of the film."},
                        "dialogue_score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "reason": {"type": "string", "description": "Why this clip would hold attention."},
                    },
                    "required": ["start", "end", "title", "category", "interest_score",
                                 "context_score", "dialogue_score"],
                },
            }
        },
        "required": ["candidates"],
    },
}

_VISION_TOOL = {
    "name": "record_visual_interest",
    "description": "Rate each contact sheet for how visually interesting the scene looks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ratings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "The scene number given with the image."},
                        "visual_interest": {"type": "integer", "minimum": 0, "maximum": 100},
                        "note": {"type": "string"},
                    },
                    "required": ["index", "visual_interest"],
                },
            }
        },
        "required": ["ratings"],
    },
}

SYSTEM_PROMPT = """\
You are a film editor choosing short clips for a social feed from a full-length film.

You will be given a transcript excerpt with timestamps in seconds from the start
of the film. Choose the moments in it that would work as standalone short clips
of roughly 20 to 75 seconds.

Strong candidates: emotionally intense scenes, funny exchanges, iconic or highly
quotable dialogue, arguments, reveals and twists, suspense, inspirational
moments, romantic beats, and action the dialogue makes clear.

Reject: exposition that needs the whole film to mean anything, fragments of a
sentence, moments whose payoff lies outside the excerpt, intros, credits, long
silences, and near-duplicates of a scene you already chose.

Rules:
- start and end are absolute seconds from the film's beginning, never relative
  to the excerpt.
- Start at a natural entry point (a cut, a line beginning) and end after the
  payoff lands, not in the middle of a reaction.
- Prefer few, excellent candidates over many mediocre ones.
- The title must be a title, not a description. The hook must be the actual
  line or beat that grabs a viewer.
- If the excerpt has nothing worth clipping, return an empty list."""


class AnalysisError(Exception):
    """The model could not be reached or would not answer usefully.

    `transient` marks the failures worth waiting out — an overloaded or briefly
    unreachable endpoint — as opposed to one that will fail the same way every
    time, like a rejected key.
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


# Statuses that mean "not now" rather than "not ever": rate limits, gateways
# and the Cloudflare family (520-524, 529) that sits in front of many endpoints.
TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}


def _clean_error_body(text: str, limit: int = 200) -> str:
    """A provider's error, cut down to something worth showing a person.

    A gateway failure arrives as a full HTML page — Cloudflare's 522 alone is
    several kilobytes of markup — and printing that into a job's notes tells
    nobody anything. The page title carries the useful part ("... | 522:
    Connection timed out").
    """
    body = (text or "").strip()
    if not body:
        return ""
    if "<html" in body.lower() or "<!doctype" in body.lower():
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.DOTALL | re.IGNORECASE)
        body = title.group(1) if title else "the endpoint returned an HTML error page"
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit]


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-5"
    vision_model: str = "claude-haiku-4-5-20251001"
    timeout: int = 180
    max_tokens: int = 8000
    attempts: int = 3
    # A flaky gateway is given more chances than a rejected request.
    transient_attempts: int = 6
    proxy: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def vision_configured(self) -> bool:
        return bool(self.api_key and self.vision_model)

    @property
    def proxies(self) -> dict | None:
        """An explicit proxy for model traffic, when the endpoint needs one.

        Set deliberately rather than inherited from the environment: the
        server also talks to Jellyfin and the library mount on localhost, and
        an ambient HTTP_PROXY would send those through it too.
        """
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    @classmethod
    def from_env(cls) -> "LLMConfig":
        api_key = (
            os.environ.get("POPCORN_LLM_API_KEY")
            or os.environ.get("POPCORN_ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or ""
        ).strip()
        base_url = (
            os.environ.get("POPCORN_LLM_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com"
        ).strip()
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=os.environ.get("POPCORN_LLM_MODEL", cls.model).strip() or cls.model,
            vision_model=os.environ.get("POPCORN_VISION_MODEL", cls.vision_model).strip(),
            proxy=os.environ.get("POPCORN_LLM_PROXY", "").strip(),
        )


# ── The model call ──────────────────────────────────────────────────────────


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/") + "/"
    if base.endswith("/v1/"):
        return urljoin(base, "messages")
    return urljoin(base, "v1/messages")


def _post(config: LLMConfig, body: dict) -> dict:
    response = requests.post(
        _endpoint(config.base_url),
        headers={
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=config.timeout,
        proxies=config.proxies,
    )
    if response.status_code >= 400:
        raise AnalysisError(
            f"The model endpoint answered {response.status_code} "
            f"({_clean_error_body(response.text) or 'no explanation'})",
            transient=response.status_code in TRANSIENT_STATUSES,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AnalysisError("The model returned a response that was not JSON.") from exc


def _tool_input(payload: dict, tool_name: str) -> dict:
    """The structured half of a reply, whichever way the provider returned it."""
    for block in payload.get("content") or []:
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            if isinstance(block.get("input"), dict):
                return block["input"]
    # A provider that ignores tool_choice may still have written the JSON out
    # as text; salvage it rather than losing the whole chunk.
    text = "".join(
        block.get("text") or "" for block in payload.get("content") or []
        if block.get("type") == "text"
    )
    if text.strip():
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
    raise AnalysisError("The model did not return the structured data it was asked for.")


def _call_with_retries(config: LLMConfig, body: dict, tool_name: str, *, on_note=None) -> dict:
    """Call the model, waiting out the failures that are worth waiting out.

    A job runs in the background with nobody watching it, so patience is cheap:
    an endpoint that is briefly unreachable gets a longer ladder of retries
    than one that has rejected the request outright.
    """
    attempt = 0
    budget = config.attempts
    last_error: Exception | None = None

    while attempt < budget:
        try:
            return _tool_input(_post(config, body), tool_name)
        except (AnalysisError, requests.RequestException) as exc:
            last_error = exc
            transient = (
                getattr(exc, "transient", False)
                or isinstance(exc, (requests.Timeout, requests.ConnectionError))
            )
            if transient:
                budget = max(budget, config.transient_attempts)
            attempt += 1
            if attempt >= budget:
                break
            delay = min(30.0, 2.0 ** attempt) + random.random() * 2 if transient else float(attempt)
            if on_note:
                on_note(f"{exc} Retrying in {delay:.0f}s…")
            time.sleep(delay)

    raise AnalysisError(str(last_error) if last_error else "The model call failed.")


# ── Chunking ────────────────────────────────────────────────────────────────


def chunk_segments(segments: list, *, max_characters: int = CHUNK_CHARACTERS,
                   overlap: int = CHUNK_OVERLAP_SEGMENTS) -> list[list]:
    """Split a transcript into overlapping, segment-aligned chunks."""
    chunks: list[list] = []
    current: list = []
    length = 0
    for segment in segments:
        current.append(segment)
        length += len(segment.text) + 16
        if length >= max_characters:
            chunks.append(current)
            current = current[-overlap:] if overlap else []
            length = sum(len(s.text) + 16 for s in current)
    if current and (not chunks or current is not chunks[-1]):
        # Only append a tail chunk if it adds anything beyond the last one.
        if not chunks or len(current) > overlap:
            chunks.append(current)
    return chunks


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"


def _excerpt(segments: list) -> str:
    return "\n".join(f"[{_timestamp(s.start)}] {s.text}" for s in segments)


def _movie_context(meta: dict) -> str:
    parts = [meta.get("title") or "An untitled film"]
    if meta.get("year"):
        parts.append(f"({meta['year']})")
    line = " ".join(parts)
    if meta.get("genres"):
        line += f" — {', '.join(meta['genres'][:4])}"
    if meta.get("runtime_seconds"):
        line += f" — {_timestamp(meta['runtime_seconds'])} long"
    return line


# ── Candidate validation ────────────────────────────────────────────────────


def _score(value, default: float = 50.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(100.0, number))


def _text(value, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip().strip('"“”')
    return cleaned[:limit]


def validate_candidates(raw: dict, *, duration: float | None,
                        min_seconds: float = MIN_CANDIDATE_SECONDS) -> list[dict]:
    """Turn whatever the model produced into candidates that can be trusted.

    Malformed entries are dropped rather than repaired into something wrong,
    and times are clamped to the film instead of being believed.
    """
    candidates: list[dict] = []
    items = raw.get("candidates")
    if not isinstance(items, list):
        return candidates
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if start != start or end != end or end <= start:
            continue
        if duration:
            start = max(0.0, min(start, max(0.0, duration - min_seconds)))
            end = min(end, duration)
        if end - start < min_seconds:
            continue
        category = str(item.get("category") or "other").strip().lower()
        if category not in CATEGORIES:
            category = "other"
        title = _text(item.get("title"), 80)
        if not title:
            continue
        candidates.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "title": title,
            "summary": _text(item.get("summary"), 240),
            "hook": _text(item.get("hook"), 160),
            "category": category,
            "interest_score": _score(item.get("interest_score")),
            "context_score": _score(item.get("context_score")),
            "dialogue_score": _score(item.get("dialogue_score")),
            "reason": _text(item.get("reason"), 240),
        })
    return candidates


def find_candidates(transcript, meta: dict, config: LLMConfig, *, on_progress=None,
                    on_note=None) -> tuple[list[dict], bool]:
    """Ask the model for candidate scenes, chunk by chunk.

    Returns the candidates and whether every excerpt was read. A chunk that
    fails does not throw away the rest of the film, but it does mean the result
    is incomplete — which the caller must not store as if it were finished.
    """
    if not config.configured:
        raise AnalysisError(
            "No model credential is configured. Add POPCORN_LLM_API_KEY to "
            "/etc/popcorn.env and restart Popcorn."
        )
    segments = transcript.segments
    chunks = chunk_segments(segments)
    if not chunks:
        raise AnalysisError("There is no dialogue to analyse in this movie.")

    duration = meta.get("runtime_seconds")
    context = _movie_context(meta)
    candidates: list[dict] = []
    failures = 0

    for index, chunk in enumerate(chunks):
        if on_progress:
            on_progress(index, len(chunks), f"Reading the transcript ({index + 1} of {len(chunks)})")
        prompt = (
            f"Film: {context}\n"
            f"Excerpt {index + 1} of {len(chunks)}, covering "
            f"{_timestamp(chunk[0].start)} to {_timestamp(chunk[-1].end)}.\n\n"
            f"Transcript:\n{_excerpt(chunk)}\n\n"
            f"Choose up to {MAX_CANDIDATES_PER_CHUNK} scenes from this excerpt that would make "
            "good standalone short clips. Use absolute timestamps."
        )
        body = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": [_CANDIDATE_TOOL],
            "tool_choice": {"type": "tool", "name": _CANDIDATE_TOOL["name"]},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            raw = _call_with_retries(config, body, _CANDIDATE_TOOL["name"], on_note=on_note)
        except AnalysisError as exc:
            # One bad excerpt should not throw away the rest of the film.
            failures += 1
            if failures == len(chunks):
                raise
            if on_note:
                on_note(f"Excerpt {index + 1} of {len(chunks)} could not be read ({exc}).")
            continue
        candidates.extend(validate_candidates(raw, duration=duration))

    if on_progress:
        on_progress(len(chunks), len(chunks), "Reading the transcript")
    return candidates, failures == 0


# ── Optional targeted visual review ─────────────────────────────────────────


def visual_review(shots: list[dict], config: LLMConfig, *, on_note=None) -> dict:
    """Rate contact sheets for the scenes the transcript liked most.

    `shots` is a list of `{"key", "image" (path), "label"}`. Only the top
    candidates ever get here, and only when the vision pass is enabled; a
    failure just means ranking continues on the transcript alone.
    """
    import base64

    if not shots:
        return {}
    if not config.vision_configured:
        return {}

    content: list[dict] = [{
        "type": "text",
        "text": (
            "These are contact sheets from candidate clips of one film, three frames "
            "each (start, middle, end), in order. Rate each scene for how visually "
            "interesting it is to watch — staging, faces, action, lighting, movement. "
            "A static two-hander is fine if it is performed; a dark empty room is not. "
            "Use the scene number shown with each image."
        ),
    }]
    for index, shot in enumerate(shots):
        try:
            data = base64.b64encode(open(shot["image"], "rb").read()).decode("ascii")
        except OSError:
            continue
        content.append({"type": "text", "text": f"Scene {index}: {shot.get('label') or ''}"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
        })
    if len(content) <= 1:
        return {}

    body = {
        "model": config.vision_model,
        "max_tokens": 2000,
        "tools": [_VISION_TOOL],
        "tool_choice": {"type": "tool", "name": _VISION_TOOL["name"]},
        "messages": [{"role": "user", "content": content}],
    }
    try:
        raw = _call_with_retries(config, body, _VISION_TOOL["name"], on_note=on_note)
    except AnalysisError as exc:
        if on_note:
            on_note(f"Visual review skipped: {exc}")
        return {}

    ratings: dict[str, float] = {}
    for entry in raw.get("ratings") or []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(shots):
            ratings[shots[index]["key"]] = _score(entry.get("visual_interest"), 50.0)
    return ratings


# ── Ranking ─────────────────────────────────────────────────────────────────


def _overlap_ratio(first: dict, second: dict) -> float:
    start = max(first["start"], second["start"])
    end = min(first["end"], second["end"])
    if end <= start:
        return 0.0
    shortest = min(first["end"] - first["start"], second["end"] - second["start"])
    return (end - start) / shortest if shortest > 0 else 0.0


def _edge_penalty(candidate: dict, duration: float | None) -> float:
    if not duration:
        return 0.0
    window = duration * EDGE_WINDOW
    if candidate["start"] < window:
        return 1.0 - (candidate["start"] / window) if window else 1.0
    if candidate["end"] > duration - window:
        return 1.0 - ((duration - candidate["end"]) / window) if window else 1.0
    return 0.0


def final_score(candidate: dict, *, duration: float | None = None) -> float:
    """One number for a candidate, blending model judgement with structure."""
    category = CATEGORY_WEIGHT.get(candidate.get("category", "other"), 0.5)
    visual = candidate.get("visual_score")
    parts = {
        "interest": candidate.get("interest_score", 50.0),
        "dialogue": candidate.get("dialogue_score", 50.0),
        "self_contained": candidate.get("context_score", 50.0),
        "category": category * 100,
        "boundary": candidate.get("boundary_quality", 0.75) * 100,
    }
    weights = dict(SCORE_WEIGHTS)
    if visual is None:
        # No vision pass ran, so its share is redistributed over the signals
        # that did rather than scoring every clip as merely average.
        weights.pop("visual")
    else:
        parts["visual"] = visual
    total_weight = sum(weights.values())
    score = sum(parts[name] * weight for name, weight in weights.items()) / total_weight
    score -= _edge_penalty(candidate, duration) * 25
    score -= max(0.0, 60.0 - candidate.get("context_score", 60.0)) * 0.25
    return round(max(0.0, min(100.0, score)), 2)


def rank_candidates(candidates: list[dict], *, duration: float | None,
                    target_count: int, minimum_score: float = 55.0) -> list[dict]:
    """Pick the best clips: scored, de-duplicated, and trimmed to the target."""
    scored = []
    seen: set[tuple] = set()
    for candidate in candidates:
        key = (round(candidate["start"], 1), round(candidate["end"], 1), candidate["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        candidate = dict(candidate)
        candidate["final_score"] = final_score(candidate, duration=duration)
        scored.append(candidate)

    scored.sort(key=lambda item: item["final_score"], reverse=True)

    kept: list[dict] = []
    for candidate in scored:
        if candidate["final_score"] < minimum_score:
            continue
        if any(_overlap_ratio(candidate, chosen) > MAX_OVERLAP for chosen in kept):
            continue
        kept.append(candidate)
        if len(kept) >= target_count:
            break

    kept.sort(key=lambda item: item["start"])
    return kept
