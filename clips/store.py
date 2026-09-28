"""Where generated clips and their analysis are remembered.

The library itself lives in Jellyfin and on Drive; this database only holds
what Popcorn produced:

* `clip_analyses` — one row per movie, holding the timestamped transcript and
  the scenes the model picked from it. This is the expensive part of the job,
  so it is cached keyed by the transcript and source-file fingerprints: a
  second run with unchanged inputs never pays for the same reasoning twice and
  never needs the model at all.
* `clips` — one row per rendered clip, so the detail page can list clips
  without reading Drive.

The schema is created alongside the download tables by `web_app._init_state`,
and the database path is bound there too, so both halves of the application
share one file.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "popcorn.sqlite3"


def bind(path: Path | str) -> None:
    """Point this module at the application's database."""
    global STATE_FILE
    STATE_FILE = Path(path)


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(STATE_FILE, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    # See web_app._database: WAL with NORMAL keeps a commit off the fsync path.
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.row_factory = sqlite3.Row
    return connection


def ensure_schema() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _database() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS clip_analyses ("
            "item_id TEXT PRIMARY KEY,"
            "analysis_version INTEGER NOT NULL,"
            "source_path TEXT NOT NULL,"
            "source_hash TEXT NOT NULL,"
            "duration REAL NOT NULL,"
            "transcript_hash TEXT NOT NULL,"
            "transcript_source TEXT NOT NULL,"
            "transcript_detail TEXT,"
            "transcript TEXT NOT NULL,"
            "candidates TEXT NOT NULL,"
            "refined TEXT NOT NULL,"
            "clips_dir TEXT,"
            "created_at REAL NOT NULL,"
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS clips ("
            "clip_id TEXT PRIMARY KEY,"
            "item_id TEXT NOT NULL,"
            "position INTEGER NOT NULL,"
            "title TEXT NOT NULL,"
            "summary TEXT,"
            "hook TEXT,"
            "category TEXT,"
            "reason TEXT,"
            "start_seconds REAL NOT NULL,"
            "end_seconds REAL NOT NULL,"
            "duration REAL NOT NULL,"
            "interest_score REAL,"
            "context_score REAL,"
            "dialogue_score REAL,"
            "visual_score REAL,"
            "final_score REAL,"
            "boundary_quality REAL,"
            "subtitle_segments TEXT,"
            "local_dir TEXT NOT NULL,"
            "file_name TEXT NOT NULL,"
            "thumb_name TEXT,"
            "srt_name TEXT,"
            "profile TEXT NOT NULL,"
            "processing_version INTEGER NOT NULL,"
            "size_bytes INTEGER,"
            "created_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS clips_by_item ON clips(item_id, position)"
        )


# ── Analysis cache ──────────────────────────────────────────────────────────


def load_analysis(item_id: str) -> dict | None:
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM clip_analyses WHERE item_id=?", (item_id,)
        ).fetchone()
    if not row:
        return None
    analysis = dict(row)
    for key in ("transcript", "candidates", "refined"):
        try:
            analysis[key] = json.loads(analysis[key])
        except (TypeError, ValueError):
            return None
    return analysis


def save_analysis(
    *,
    item_id: str,
    analysis_version: int,
    source_path: str,
    source_hash: str,
    duration: float,
    transcript_hash: str,
    transcript_source: str,
    transcript_detail: str,
    transcript: list[dict],
    candidates: list[dict],
    refined: list[dict],
    clips_dir: str | None = None,
) -> None:
    now = time.time()
    with _database() as connection:
        connection.execute(
            "INSERT INTO clip_analyses("
            "item_id, analysis_version, source_path, source_hash, duration,"
            "transcript_hash, transcript_source, transcript_detail, transcript,"
            "candidates, refined, clips_dir, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET "
            "analysis_version=excluded.analysis_version, source_path=excluded.source_path,"
            "source_hash=excluded.source_hash, duration=excluded.duration,"
            "transcript_hash=excluded.transcript_hash, transcript_source=excluded.transcript_source,"
            "transcript_detail=excluded.transcript_detail, transcript=excluded.transcript,"
            "candidates=excluded.candidates, refined=excluded.refined,"
            "clips_dir=COALESCE(excluded.clips_dir, clip_analyses.clips_dir),"
            "updated_at=excluded.updated_at",
            (
                item_id, analysis_version, source_path, source_hash, duration,
                transcript_hash, transcript_source, transcript_detail,
                json.dumps(transcript), json.dumps(candidates), json.dumps(refined),
                clips_dir, now, now,
            ),
        )


def set_analysis_clips_dir(item_id: str, clips_dir: str) -> None:
    with _database() as connection:
        connection.execute(
            "UPDATE clip_analyses SET clips_dir=?, updated_at=? WHERE item_id=?",
            (clips_dir, time.time(), item_id),
        )


def delete_analysis(item_id: str) -> None:
    """Forget a movie's analysis, so the next run asks the model again."""
    with _database() as connection:
        connection.execute("DELETE FROM clip_analyses WHERE item_id=?", (item_id,))


# ── Clips ───────────────────────────────────────────────────────────────────


def replace_clips(item_id: str, clips: list[dict]) -> None:
    """Store a freshly rendered set of clips, replacing any earlier set.

    Regenerating a movie replaces its clips rather than accumulating them:
    two generations of the same scenes would be indistinguishable in the
    interface, and the Drive folder is rewritten in the same pass.
    """
    with _database() as connection:
        connection.execute("DELETE FROM clips WHERE item_id=?", (item_id,))
        connection.executemany(
            "INSERT INTO clips("
            "clip_id, item_id, position, title, summary, hook, category, reason,"
            "start_seconds, end_seconds, duration, interest_score, context_score,"
            "dialogue_score, visual_score, final_score, boundary_quality,"
            "subtitle_segments, local_dir, file_name, thumb_name, srt_name,"
            "profile, processing_version, size_bytes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    clip["clip_id"], item_id, clip["position"], clip["title"],
                    clip.get("summary"), clip.get("hook"), clip.get("category"),
                    clip.get("reason"), clip["start"], clip["end"], clip["duration"],
                    clip.get("interest_score"), clip.get("context_score"),
                    clip.get("dialogue_score"), clip.get("visual_score"),
                    clip.get("final_score"), clip.get("boundary_quality"),
                    json.dumps(clip.get("subtitle_segments") or []),
                    clip["local_dir"], clip["file_name"], clip.get("thumb_name"),
                    clip.get("srt_name"), clip.get("profile", "original"),
                    clip.get("processing_version", 1), clip.get("size_bytes"),
                    clip.get("created_at") or time.time(),
                )
                for clip in clips
            ],
        )


def load_clips(item_id: str) -> list[dict]:
    with _database() as connection:
        rows = connection.execute(
            "SELECT * FROM clips WHERE item_id=? ORDER BY position", (item_id,)
        ).fetchall()
    return [_clip_row(row) for row in rows]


def clip(clip_id: str) -> dict | None:
    with _database() as connection:
        row = connection.execute("SELECT * FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
    return _clip_row(row) if row else None


def delete_clips(item_id: str) -> None:
    with _database() as connection:
        connection.execute("DELETE FROM clips WHERE item_id=?", (item_id,))


def _clip_row(row: sqlite3.Row) -> dict:
    clip = dict(row)
    try:
        clip["subtitle_segments"] = json.loads(clip.get("subtitle_segments") or "[]")
    except (TypeError, ValueError):
        clip["subtitle_segments"] = []
    return clip
