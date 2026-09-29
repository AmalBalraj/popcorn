#!/usr/bin/env python3
"""Popcorn web application: find, download, and watch movies."""

from __future__ import annotations

import json
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import os
import re
import signal
import fcntl
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

import requests
from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for
from werkzeug.security import check_password_hash

import popcorn
import media_library
import subtitle_downloads
from job_transfer import Transfer, stop_transfer, PROGRESS_PATTERN
from clips import analysis as clip_analysis
from clips import pipeline as clip_pipeline
from clips import render as clip_render
from clips import store as clip_store
from clips import transcript as clip_transcript


app = Flask(__name__)
app.secret_key = os.environ.get("POPCORN_SECRET_KEY", "development-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("POPCORN_SECURE_COOKIE", "0") == "1",
)

LOGIN_USERNAME = os.environ.get("POPCORN_USERNAME", "amal")
LOGIN_PASSWORD_HASH = os.environ.get("POPCORN_PASSWORD_HASH", "")
DEFAULT_UPLOAD_TO = os.environ.get("POPCORN_DEFAULT_UPLOAD", "gdrive:Movies")
JELLYFIN_URL = os.environ.get("POPCORN_JELLYFIN_URL", "http://127.0.0.1:8096/jellyfin").rstrip("/")
JELLYFIN_USERNAME = os.environ.get("POPCORN_JELLYFIN_USERNAME", "").strip()
JELLYFIN_PASSWORD = os.environ.get("POPCORN_JELLYFIN_PASSWORD", "")
JELLYFIN_API_KEY = os.environ.get("POPCORN_JELLYFIN_API_KEY", "").strip()
TMDB_API_KEY = os.environ.get("POPCORN_TMDB_API_KEY", "").strip()
LIBRARY_PATH = Path(os.environ.get("POPCORN_LIBRARY_PATH", "/home/amal/gdrive-movies"))
LIBRARY_REMOTE = os.environ.get("POPCORN_LIBRARY_REMOTE", "gdrive:Movies").rstrip("/")
RCLONE_RC_URL = os.environ.get("POPCORN_RCLONE_RC_URL", "http://127.0.0.1:5572/")
SHOWS_PATH = Path(os.environ.get("POPCORN_SHOWS_PATH", "/home/amal/gdrive-shows"))
SHOWS_REMOTE = os.environ.get("POPCORN_SHOWS_REMOTE", "gdrive:TV Shows").rstrip("/")
SHOWS_RC_URL = os.environ.get("POPCORN_SHOWS_RC_URL", "http://127.0.0.1:5573/")

SEARCHES: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
JOB_PROCESSES: dict[str, subprocess.Popen] = {}
JOB_WORKERS: set[str] = set()
JOB_RETRY_BASE = float(os.environ.get("POPCORN_JOB_RETRY_BASE", "30"))
JOB_RETRY_MAX = float(os.environ.get("POPCORN_JOB_RETRY_MAX", "1800"))
JOB_MAX_WORKERS = int(os.environ.get("POPCORN_JOB_MAX_WORKERS", "3"))
STATE_LOCK = threading.Lock()
ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / "data" / "settings.json"
STATE_FILE = Path(os.environ.get("POPCORN_STATE_FILE", str(ROOT / "data" / "popcorn.sqlite3")))
DEFAULT_SETTINGS = {
    "default_upload": DEFAULT_UPLOAD_TO,
    "default_source": "all",
    "private_dns": False,
    "search_timeout": 15,
    "subtitle_language": "eng",
    "subtitle_mode": "Always",
    "subtitle_auto_download": True,
    "clip_target_count": clip_pipeline.DEFAULT_SETTINGS["clip_target_count"],
    "clip_min_seconds": clip_pipeline.DEFAULT_SETTINGS["clip_min_seconds"],
    "clip_max_seconds": clip_pipeline.DEFAULT_SETTINGS["clip_max_seconds"],
    "clip_vision": clip_pipeline.DEFAULT_SETTINGS["clip_vision"],
    "clip_subtitle_download": clip_pipeline.DEFAULT_SETTINGS["clip_subtitle_download"],
    "clip_whisper_model": clip_pipeline.DEFAULT_SETTINGS["clip_whisper_model"],
}

TERMINAL_JOB_STATES = {"complete", "failed", "interrupted", "cancelled"}
ACTIVE_JOB_STATES = {"queued", "resolving", "downloading", "identifying", "subtitles", "uploading", "indexing", "cancelling"}
# Clip jobs share the jobs table and the Stop plumbing, but their stages are
# their own: nothing about a clip job looks like a download.
CLIP_ACTIVE_STATES = {
    clip_pipeline.QUEUED, clip_pipeline.EXTRACTING_SUBTITLES, clip_pipeline.TRANSCRIBING,
    clip_pipeline.ANALYZING_TRANSCRIPT, clip_pipeline.FINDING_SCENES,
    clip_pipeline.RANKING_SCENES, clip_pipeline.GENERATING_CLIPS, clip_pipeline.UPLOADING,
}
CLIP_TERMINAL_STATES = {
    clip_pipeline.COMPLETED, clip_pipeline.FAILED, clip_pipeline.CANCELLED, "interrupted",
}
# One clip job at a time: it can saturate every core on this server, and the
# library is shared, so a second job would only make both slower.
CLIP_JOBS_ALLOWED = 1
VIDEO_EXTENSIONS = {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm", ".wmv"}
_ITEM_ID = re.compile(r"[a-fA-F0-9-]{32,36}")
# How long after a client's last playback report a session still counts as
# live. Jellyfin never expires NowPlayingItem on its own.
PLAYBACK_GRACE_SECONDS = 180


class DownloadCancelled(Exception):
    pass


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(STATE_FILE, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    # NORMAL in WAL mode takes the fsync out of a commit. Job rows are progress
    # records, so a power cut costing the last few seconds of them is nothing —
    # but a commit that has to wait on a disk busy writing a download is how a
    # progress update ends up outliving its busy timeout and failing the very
    # transfer it describes.
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _init_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _database() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        rows = connection.execute("SELECT payload FROM jobs ORDER BY created_at DESC").fetchall()
    clip_store.bind(STATE_FILE)
    clip_store.ensure_schema()
    for (payload,) in rows:
        try:
            saved = json.loads(payload)
            if saved.get("status") not in TERMINAL_JOB_STATES | CLIP_TERMINAL_STATES:
                if saved.get("kind") == "clips":
                    # A clip job's working files are worthless once the process
                    # is gone: the analysis cache is what makes a re-run cheap,
                    # and the next run reuses it.
                    _remove_clip_work_dir(saved.get("work_dir"))
                    saved.update({
                        "status": clip_pipeline.FAILED,
                        "message": "The server restarted before this finished. Generate clips again to continue.",
                        "work_dir": None,
                        "updated_at": time.time(),
                    })
                else:
                    # New jobs persist stage checkpoints and selected release.
                    # Older rows retain their local files even when they cannot resume.
                    if saved.get("status") in {"uploading", "indexing"}:
                        saved["download_complete"] = True
                    if saved.get("status") == "indexing":
                        saved["upload_complete"] = True
                    if saved.get("status") == "needs_identification":
                        saved.update(retry_at=None, recoverable=False)
                    elif saved.get("release") or saved.get("download_complete"):
                        saved.update(retry_at=0, message="Recovering saved download…")
                    else:
                        saved.update(status="interrupted",
                                     message="This older job lacks recovery metadata. Local files were retained.")
                    saved["updated_at"] = time.time()
                _persist_job(saved)
            elif saved.get("speed") != "—" or saved.get("eta") != "—":
                saved.update({"speed": "—", "eta": "—"})
                _persist_job(saved)
            JOBS[saved["id"]] = saved
        except (ValueError, KeyError, TypeError):
            continue


def _remove_clip_work_dir(work_dir: str | None) -> None:
    """Delete a clip job's temporary folder, and only ever its own."""
    if not work_dir:
        return
    path = Path(work_dir).resolve()
    if path.parent == ROOT and path.name.startswith("popcorn-clips-"):
        shutil.rmtree(path, ignore_errors=True)


def _persist_job(job: dict, *, required: bool = False) -> None:
    """Save a job to disk without ever letting that save break the job.

    The running server keeps job state in memory and every page reads it from
    there; the row exists so history survives a restart. A database that is
    briefly unavailable should therefore cost one stale row — never the
    download the row describes.
    """
    for attempt in range(3):
        try:
            with _database() as connection:
                connection.execute(
                    "INSERT INTO jobs(id, payload, created_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                    (job["id"], json.dumps(job), job["created_at"], job["updated_at"]),
                )
            return
        except sqlite3.Error as exc:
            if attempt == 2:
                app.logger.error("Could not save job %s: %s", job.get("id"), exc)
                if required:
                    raise
                return
            time.sleep(0.5 * (attempt + 1))


def _meta_timestamp(key: str) -> float:
    with _database() as connection:
        row = connection.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    try:
        return float(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _set_meta_timestamp(key: str) -> None:
    with _database() as connection:
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(time.time())),
        )




def _error(message: str, status: int = 400, detail: str | None = None):
    """A failure the viewer can act on, with the raw cause kept separate.

    `message` is shown; `detail` is only revealed behind a diagnostics
    disclosure, so technical causes never lead.
    """
    payload = {"error": message}
    if detail:
        payload["detail"] = detail
    return jsonify(payload), status


def _signed_in() -> bool:
    return session.get("authenticated") is True


def _load_settings() -> dict:
    settings = DEFAULT_SETTINGS.copy()
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update({key: saved[key] for key in settings.keys() & saved.keys()})
    except (FileNotFoundError, OSError, ValueError):
        pass
    return settings


def _save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


def _jellyfin_headers(token: str | None = None, device_id: str | None = None) -> dict[str, str]:
    if device_id == "":
        # Jellyfin 12 distinguishes server API keys from device tokens by the
        # absence of Device/DeviceId in the authorization header.
        authorization = 'MediaBrowser Client="Popcorn", Version="1.0.0"'
    else:
        authorization = (
            'MediaBrowser Client="Popcorn", Device="Web", '
            f'DeviceId="{device_id or "popcorn-server"}", Version="1.0.0"'
        )
    if token:
        authorization += f', Token="{token}"'
    return {"Authorization": authorization, "Accept": "application/json"}


def _connect_jellyfin(username: str, password: str) -> tuple[dict | None, str | None]:
    device_id = f"popcorn-web-{uuid.uuid4().hex}"
    try:
        response = requests.post(
            f"{JELLYFIN_URL}/Users/AuthenticateByName",
            headers=_jellyfin_headers(device_id=device_id),
            json={"Username": username, "Pw": password},
            timeout=8,
        )
        response.raise_for_status()
        result = response.json()
        user = result["User"]
        return {
            "token": result["AccessToken"],
            "user_id": user["Id"],
            "username": user["Name"],
            "is_admin": bool(user.get("Policy", {}).get("IsAdministrator")),
            "server_id": result["ServerId"],
            "server_name": "Popcorn",
            "device_id": device_id,
        }, None
    except (requests.RequestException, KeyError, ValueError):
        return None, "Your Popcorn login worked, but the watch library could not be connected."


def _sync_jellyfin_settings(settings: dict) -> None:
    connection = session.get("jellyfin") or {}
    token, user_id = connection.get("token"), connection.get("user_id")
    device_id = connection.get("device_id")
    if not token or not user_id:
        return
    response = requests.get(
        f"{JELLYFIN_URL}/Users/{user_id}", headers=_jellyfin_headers(token, device_id), timeout=8
    )
    response.raise_for_status()
    configuration = response.json().get("Configuration", {})
    configuration["SubtitleLanguagePreference"] = settings["subtitle_language"]
    configuration["SubtitleMode"] = settings["subtitle_mode"]
    configuration["RememberSubtitleSelections"] = True
    response = requests.post(
        f"{JELLYFIN_URL}/Users/Configuration",
        params={"userId": user_id},
        headers={**_jellyfin_headers(token, device_id), "Content-Type": "application/json"},
        json=configuration,
        timeout=8,
    )
    response.raise_for_status()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _signed_in():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return _error("Your session has expired. Sign in again.", 401)
        return redirect(url_for("login", next=request.path))
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        _refresh_account_session()
        if (session.get("jellyfin") or {}).get("is_admin"):
            return view(*args, **kwargs)
        return _error("Administrator access is required.", 403)
    return wrapped


def _refresh_account_session() -> dict:
    connection = session.get("jellyfin") or {}
    token = connection.get("token")
    if token and ("is_admin" not in connection or "username" not in connection):
        try:
            response = requests.get(
                f"{JELLYFIN_URL}/Users/Me",
                headers=_jellyfin_headers(token, connection.get("device_id")),
                timeout=8,
            )
            response.raise_for_status()
            user = response.json()
            connection.update({
                "user_id": user["Id"],
                "username": user["Name"],
                "is_admin": bool(user.get("Policy", {}).get("IsAdministrator")),
            })
            session["jellyfin"] = connection
        except (requests.RequestException, KeyError, ValueError):
            pass
    return connection


@app.route("/login", methods=["GET", "POST"])
def login():
    if _signed_in():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        connection, connection_error = _connect_jellyfin(username, password)
        legacy_login = (
            LOGIN_PASSWORD_HASH
            and username == LOGIN_USERNAME
            and check_password_hash(LOGIN_PASSWORD_HASH, password)
        )
        if connection or legacy_login:
            session.clear()
            session["authenticated"] = True
            if not connection and legacy_login:
                connection, connection_error = _connect_jellyfin(
                    JELLYFIN_USERNAME or username, JELLYFIN_PASSWORD or password
                )
            if connection:
                session["jellyfin"] = connection
                try:
                    _sync_jellyfin_settings(_load_settings())
                except requests.RequestException:
                    pass
            elif connection_error:
                session["jellyfin_error"] = connection_error
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("index"))
        error = "That username or password is incorrect."
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _public_result(result: popcorn.TorrentResult, index: int) -> dict:
    return {
        "id": index,
        "title": result.title,
        "seeds": result.seeds,
        "peers": result.peers,
        "size": result.size,
        "source": result.source,
        "sources": result.sources,
        "score": result.score,
        "score_details": result.score_details,
    }


def _prune_searches() -> None:
    cutoff = time.time() - 3600
    for search_id in list(SEARCHES):
        if SEARCHES[search_id]["created_at"] < cutoff:
            SEARCHES.pop(search_id, None)


@app.get("/")
@login_required
def index():
    return render_template("home.html")


@app.get("/movies")
@login_required
def movies_page():
    return render_template(
        "browse.html",
        page="movies",
        library_type="Movie",
        heading="Movies",
        blurb="Everything you own, ready to play.",
        filter=request.args.get("filter"),
    )


@app.get("/shows")
@login_required
def shows_page():
    return render_template(
        "browse.html",
        page="shows",
        library_type="Series",
        heading="TV Shows",
        blurb="Every series in your library, with seasons and episodes ready to play.",
        filter=request.args.get("filter"),
    )


@app.get("/movie/<item_id>")
@app.get("/show/<item_id>")
@app.get("/collection/<item_id>")
@login_required
def detail_page(item_id: str):
    if not _ITEM_ID.fullmatch(item_id):
        return redirect(url_for("index"))
    return render_template("detail.html", item_id=item_id)


@app.get("/search")
@login_required
def search_page():
    return render_template("search.html")


@app.get("/downloads")
@login_required
def downloads():
    return render_template("downloads.html")


@app.get("/play/<item_id>")
@login_required
def play_page(item_id: str):
    if not _ITEM_ID.fullmatch(item_id):
        return redirect(url_for("index"))
    return render_template("player.html", item_id=item_id)


@app.get("/history")
@login_required
def history():
    return redirect(url_for("downloads"))


@app.get("/service-worker.js")
def service_worker():
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/stream")
@login_required
def stream():
    return redirect(url_for("watch"), code=301)


@app.get("/watch")
@login_required
def watch():
    item_id = request.args.get("item", "")
    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", item_id):
        item_id = ""
    return render_template(
        "watch.html",
        jellyfin=session.get("jellyfin"),
        jellyfin_error=session.get("jellyfin_error"),
        item_id=item_id,
    )


@app.get("/settings")
@login_required
def settings_page():
    account = _refresh_account_session()
    return render_template("settings.html", settings=_load_settings(), account=account,
                           torznab_available="torznab" in popcorn.source_service.providers)


@app.route("/api/settings", methods=["GET", "PUT"])
@login_required
def settings_api():
    if request.method == "GET":
        return jsonify(_load_settings())
    payload = request.get_json(silent=True) or {}
    settings = _load_settings()
    if "subtitle_auto_download" in payload and payload["subtitle_auto_download"] != settings["subtitle_auto_download"]:
        if not (_refresh_account_session() or {}).get("is_admin"):
            return _error("Administrator access is required to change automatic subtitle downloads.", 403)
    source = str(payload.get("default_source", settings["default_source"]))
    if source not in (*popcorn.source_service.providers, "all"):
        return _error("Unknown search source.")
    try:
        timeout = max(5, min(int(payload.get("search_timeout", settings["search_timeout"])), 60))
    except (TypeError, ValueError):
        return _error("Timeout must be a number between 5 and 60.")
    subtitle_mode = str(payload.get("subtitle_mode", settings["subtitle_mode"]))
    if subtitle_mode not in {"Always", "Default", "OnlyForced", "None"}:
        return _error("Unknown subtitle mode.")
    subtitle_language = str(payload.get("subtitle_language", settings["subtitle_language"])).strip().lower()
    if not re.fullmatch(r"[a-z]{3}", subtitle_language):
        return _error("Subtitle language must use a three-letter language code.")
    try:
        clip_target_count = max(3, min(int(payload.get("clip_target_count", settings["clip_target_count"])), 25))
        clip_min_seconds = max(8, min(int(payload.get("clip_min_seconds", settings["clip_min_seconds"])), 90))
        clip_max_seconds = int(payload.get("clip_max_seconds", settings["clip_max_seconds"]))
    except (TypeError, ValueError):
        return _error("Clip length and clip count must be numbers.")
    clip_max_seconds = max(clip_min_seconds + 5, min(clip_max_seconds, 180))
    whisper_model = str(payload.get("clip_whisper_model", settings["clip_whisper_model"])).strip()
    if whisper_model not in {"tiny", "base", "small", "medium", "large-v3"}:
        return _error("Unknown speech model.")
    settings.update({
        "default_upload": str(payload.get("default_upload", settings["default_upload"])).strip() or DEFAULT_UPLOAD_TO,
        "default_source": source,
        "private_dns": bool(payload.get("private_dns", settings["private_dns"])),
        "search_timeout": timeout,
        "subtitle_language": subtitle_language,
        "subtitle_mode": subtitle_mode,
        "subtitle_auto_download": bool(payload.get("subtitle_auto_download", settings["subtitle_auto_download"])),
        "clip_target_count": clip_target_count,
        "clip_min_seconds": clip_min_seconds,
        "clip_max_seconds": clip_max_seconds,
        "clip_vision": bool(payload.get("clip_vision", settings["clip_vision"])),
        "clip_subtitle_download": bool(
            payload.get("clip_subtitle_download", settings["clip_subtitle_download"])
        ),
        "clip_whisper_model": whisper_model,
    })
    _save_settings(settings)
    try:
        _sync_jellyfin_settings(settings)
    except requests.RequestException:
        return jsonify({"settings": settings, "warning": "Saved in Popcorn, but Jellyfin could not be updated."})
    return jsonify({"settings": settings})


@app.route("/api/subtitles/account", methods=["GET", "POST", "DELETE"])
@admin_required
def subtitle_account():
    if request.method == "GET":
        response = jsonify({**subtitle_downloads.provider_status(ROOT), "scan": _subtitle_scan_status()})
        response.headers["Cache-Control"] = "no-store"
        return response
    if request.method == "DELETE":
        (ROOT / "data/subtitles/account.json").unlink(missing_ok=True)
        return jsonify(subtitle_downloads.provider_status(ROOT))
    payload = request.get_json(silent=True) or {}
    credentials = {name: str(payload.get(name, "")) for name in ("username", "password", "api_key")}
    credentials["username"] = credentials["username"].strip()
    credentials["api_key"] = credentials["api_key"].strip()
    if not subtitle_downloads.configured(credentials) or any(len(value) > 1000 for value in credentials.values()):
        return _error("Enter your OpenSubtitles username, password, and API key.")
    try:
        subtitle_downloads.OpenSubtitles(credentials).login()
    except subtitle_downloads.SubtitleError as exc:
        return _error(str(exc), 400 if exc.status == "authentication_required" else 502)
    subtitle_downloads.atomic_json(ROOT / "data/subtitles/account.json", credentials)
    settings = _load_settings()
    settings["subtitle_auto_download"] = True
    _save_settings(settings)
    _start_subtitle_scan()
    return jsonify({**subtitle_downloads.provider_status(ROOT),
                    "scan": _subtitle_scan_status(),
                    "message": "Connected. Automatic downloads are on; checking existing videos for missing subtitles."})


SUBTITLE_SCAN_LOCK = threading.Lock()
SUBTITLE_SCAN_ACTIVE = False


def _subtitle_scan_status():
    try:
        result = json.loads((ROOT / "data/subtitles/scan.json").read_text())
    except (OSError, ValueError):
        return {"status": "idle"}
    if result.get("status") == "running" and not SUBTITLE_SCAN_ACTIVE:
        return {**result, "status": "interrupted", "message": "The server restarted. Run the subtitle check again."}
    return result


def _scan_library_subtitles():
    status_file = ROOT / "data/subtitles/scan.json"
    state = {"status": "running", "checked": 0, "total": 0, "downloaded": 0, "existing": 0, "no_match": 0}
    subtitle_downloads.atomic_json(status_file, state)
    language = _load_settings()["subtitle_language"]
    token = _server_jellyfin_token()
    try:
        response = requests.get(f"{JELLYFIN_URL}/Items", headers=_jellyfin_headers(token, ""),
            params={"Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Limit": 10000,
                    "Fields": "Path,ProviderIds,MediaSources,MediaStreams,RunTimeTicks,SeriesId,ProductionYear"}, timeout=30)
        response.raise_for_status()
        items = response.json().get("Items", [])
        credentials = subtitle_downloads.load_credentials(ROOT)
        client = subtitle_downloads.OpenSubtitles(credentials)
        series = {}
        try:
            manifest = json.loads((ROOT / "data/tv-migration/manifest.json").read_text())
            original_releases = {str(SHOWS_PATH / e["relative"]): Path(e["source_remote"]).stem
                                 for e in manifest.get("entries", []) if not e.get("sidecar")}
        except (OSError, ValueError):
            original_releases = {}
        sources = [(item, source) for item in items for source in item.get("MediaSources", [])]
        state["total"] = len(sources)
        with tempfile.TemporaryDirectory(prefix="subtitles-", dir=ROOT / "data/subtitles") as work:
            for item, source in sources:
                state["checked"] += 1
                state["current_title"] = item.get("SeriesName") or item.get("Name", "")
                subtitle_downloads.atomic_json(status_file, state)
                path = Path(source.get("Path", ""))
                destination = None
                for library_path, remote in ((LIBRARY_PATH, LIBRARY_REMOTE), (SHOWS_PATH, SHOWS_REMOTE)):
                    if path.is_absolute() and path.is_relative_to(library_path) and ".." not in path.parts:
                        destination = remote + "/" + str(path.relative_to(library_path).with_suffix(f".{language}.srt"))
                        break
                if not destination or path.suffix.lower() not in VIDEO_EXTENSIONS or not path.is_file():
                    continue
                providers = item.get("ProviderIds", {})
                identity = {"kind": "movie", "title": item["Name"], "year": item.get("ProductionYear"),
                            "tmdb_id": providers.get("Tmdb"), "imdb_id": providers.get("Imdb"),
                            "duration": (source.get("RunTimeTicks") or item.get("RunTimeTicks") or 0) / TICKS_PER_SECOND}
                if item["Type"] == "Episode":
                    sid = item.get("SeriesId")
                    if not sid:
                        continue
                    if sid not in series:
                        series[sid] = _server_library_item(sid, "ProviderIds,Name") or {}
                    show = series[sid]
                    coord = media_library.coordinates(path.name)
                    if item.get("ParentIndexNumber") is None or item.get("IndexNumber") is None:
                        continue
                    identity.update(kind="episode", title=show.get("Name", item.get("SeriesName", "")),
                        show_tmdb_id=show.get("ProviderIds", {}).get("Tmdb"),
                        season=item["ParentIndexNumber"], episode=item["IndexNumber"], end_episode=coord[2] if coord else None)
                release = original_releases.get(str(path), path.stem)
                result = subtitle_downloads.fetch_one(client, path, identity, release, language,
                    source.get("MediaStreams") or item.get("MediaStreams") or [], output_dir=work)
                if result["status"] == "downloaded":
                    subprocess.run(["rclone", "copyto", result["path"], destination, "--immutable",
                        "--retries=2", "--contimeout=10s", "--timeout=30s"], check=True, timeout=90,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    Path(result["path"]).unlink()
                state[result["status"]] = state.get(result["status"], 0) + 1
            state["status"] = "complete"
    except subtitle_downloads.SubtitleError as exc:
        state.update(status=exc.status, message=str(exc))
    except (requests.RequestException, subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
        state.update(status="unavailable", message="The subtitle check stopped temporarily. Run it again to continue.")
    finally:
        if state["downloaded"]:
            _refresh_library_mount()
            _refresh_library()
        state.pop("current_title", None)
        subtitle_downloads.atomic_json(status_file, state)


def _start_subtitle_scan():
    global SUBTITLE_SCAN_ACTIVE
    if not SUBTITLE_SCAN_LOCK.acquire(blocking=False):
        return False
    SUBTITLE_SCAN_ACTIVE = True
    try:
        subtitle_downloads.atomic_json(ROOT / "data/subtitles/scan.json",
            {"status": "running", "checked": 0, "total": 0, "downloaded": 0})
    except OSError:
        SUBTITLE_SCAN_ACTIVE = False
        SUBTITLE_SCAN_LOCK.release()
        raise
    def run():
        global SUBTITLE_SCAN_ACTIVE
        try:
            _scan_library_subtitles()
        finally:
            SUBTITLE_SCAN_ACTIVE = False
            SUBTITLE_SCAN_LOCK.release()
    threading.Thread(target=run, daemon=True, name="subtitle-library-check").start()
    return True


@app.post("/api/subtitles/scan")
@admin_required
def subtitle_scan():
    if not subtitle_downloads.provider_status(ROOT)["connected"]:
        return _error("Connect OpenSubtitles first.")
    if not _server_jellyfin_token():
        return _error("The media library is not connected.", 503)
    started = _start_subtitle_scan()
    return jsonify({"started": started, "scan": _subtitle_scan_status()}), 202


def _jellyfin_request(method: str, path: str, **kwargs) -> requests.Response:
    token = (session.get("jellyfin") or {}).get("token")
    if not token:
        raise requests.RequestException("The watch library is not connected.")
    headers = kwargs.pop("headers", {})
    return requests.request(
        method,
        f"{JELLYFIN_URL}/{path.lstrip('/')}",
        headers={**_jellyfin_headers(token, (session.get("jellyfin") or {}).get("device_id")), **headers},
        timeout=kwargs.pop("timeout", 12),
        **kwargs,
    )


def _refresh_library_mount() -> None:
    """Best-effort refresh of the read-only rclone mount's directory cache."""
    for rc_url in (RCLONE_RC_URL, SHOWS_RC_URL):
        try:
            subprocess.run(
                ["rclone", "rc", "vfs/refresh", "recursive=true", "--url", rc_url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            pass


@app.get("/api/account")
@login_required
def account():
    connection = session.get("jellyfin") or {}
    return jsonify({
        "username": connection.get("username", LOGIN_USERNAME),
        "is_admin": bool(connection.get("is_admin")),
        "library_connected": bool(connection.get("token")),
    })


@app.post("/api/account/password")
@login_required
def change_password():
    payload = request.get_json(silent=True) or {}
    current_password = str(payload.get("current_password", ""))
    new_password = str(payload.get("new_password", ""))
    if len(new_password) < 8:
        return _error("Use at least eight characters for the new password.")
    try:
        response = _jellyfin_request(
            "POST",
            "Users/Password",
            params={"userId": (session.get("jellyfin") or {}).get("user_id")},
            json={"CurrentPw": current_password, "NewPw": new_password, "ResetPassword": False},
        )
        if response.status_code in {400, 401, 403}:
            return _error("The current password is incorrect.", 403)
        response.raise_for_status()
        return jsonify({"message": "Password changed."})
    except requests.RequestException:
        return _error("Could not change the password.", 502)


@app.route("/api/accounts", methods=["GET", "POST"])
@admin_required
def accounts():
    try:
        if request.method == "GET":
            response = _jellyfin_request("GET", "Users")
            response.raise_for_status()
            current_id = (session.get("jellyfin") or {}).get("user_id", "").lower()
            return jsonify({"users": [{
                "id": user["Id"],
                "name": user["Name"],
                "is_admin": bool(user.get("Policy", {}).get("IsAdministrator")),
                "is_current": user["Id"].lower() == current_id,
            } for user in response.json()]})

        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        password = str(payload.get("password", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.@+-]{2,32}", name):
            return _error("Usernames must be 2–32 letters, numbers, or . _ @ + - characters.")
        if len(password) < 8:
            return _error("Use at least eight characters for the password.")
        response = _jellyfin_request("POST", "Users/New", json={"Name": name, "Password": password})
        if response.status_code == 400:
            return _error("That username already exists.", 409)
        response.raise_for_status()
        user = response.json()
        policy = user.get("Policy", {})
        policy.update({
            "IsAdministrator": False,
            "EnableContentDeletion": False,
            "EnableContentDeletionFromFolders": [],
            "IsDisabled": False,
        })
        policy_response = _jellyfin_request("POST", f"Users/{user['Id']}/Policy", json=policy)
        policy_response.raise_for_status()
        return jsonify({"id": user["Id"], "name": user["Name"]}), 201
    except (requests.RequestException, KeyError, ValueError):
        return _error("Could not update accounts.", 502)


@app.delete("/api/accounts/<user_id>")
@admin_required
def delete_account(user_id: str):
    current_id = (session.get("jellyfin") or {}).get("user_id", "")
    if user_id.lower() == current_id.lower():
        return _error("You cannot delete the account you are using.")
    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", user_id):
        return _error("Invalid account.")
    try:
        response = _jellyfin_request("DELETE", f"Users/{user_id}")
        response.raise_for_status()
        return ("", 204)
    except requests.RequestException:
        return _error("Could not delete that account.", 502)


@app.post("/api/account/history/clear")
@login_required
def clear_watch_history():
    user_id = (session.get("jellyfin") or {}).get("user_id")
    if not user_id:
        return _error("The watch library is not connected.", 409)
    item_ids: set[str] = set()
    try:
        for filters in ("IsPlayed", "IsResumable"):
            response = _jellyfin_request(
                "GET", f"Users/{user_id}/Items",
                params={"Recursive": "true", "IncludeItemTypes": "Movie,Episode", "Filters": filters, "Limit": 10000},
            )
            response.raise_for_status()
            item_ids.update(item["Id"] for item in response.json().get("Items", []))
        for item_id in item_ids:
            response = _jellyfin_request(
                "DELETE", f"UserPlayedItems/{item_id}", params={"userId": user_id}
            )
            response.raise_for_status()
        return jsonify({"cleared": len(item_ids)})
    except (requests.RequestException, KeyError, ValueError):
        return _error("Could not clear watch history.", 502)


def _tmdb_get(path: str, **params) -> dict:
    return media_library.tmdb_get(TMDB_API_KEY, path, ROOT / "data" / "media-cache", **params)


def _clean_movie_title(title: str) -> tuple[str, int | None]:
    # Release-site prefixes are stripped first: normalising separators turns
    # "www.UIndex.org -" into "www UIndex org -", after which the pattern no
    # longer matches and the site name survives into the title.
    cleaned = re.sub(r"^(?:copy of\s+|www\.\S+\s*-\s*)", "", title.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"[._]+", " ", cleaned)
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", cleaned)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        cleaned = cleaned[:year_match.start()]
    cleaned = re.sub(
        r"\b(?:2160p|1080p|720p|480p|bluray|webrip|web-dl|hdrip|dvdrip|x26[45]|hevc|imax)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" -[]()"), year


SEARCH_ALIASES = {
    # Popular song-name searches should still find their parent feature.
    "idhayam love": ["Meyaadha Maan 2017", "Idhayam 1991"],
    "idhayam love meyaadha maan": ["Meyaadha Maan 2017"],
}


def _search_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


TITLE_ALIASES: dict[str, tuple[float, list[str]]] = {}
TITLE_ALIAS_INFLIGHT: set[str] = set()


def _title_aliases(query):
    """TMDB enrichment runs off the source-search latency path."""
    key = _search_key(query)
    with STATE_LOCK:
        cached = TITLE_ALIASES.get(key)
        if cached and time.time() - cached[0] < 1800:
            return list(cached[1])
        if not TMDB_API_KEY or key in TITLE_ALIAS_INFLIGHT or len(TITLE_ALIAS_INFLIGHT) >= 2:
            return list(cached[1]) if cached else []
        TITLE_ALIAS_INFLIGHT.add(key)
    def enrich():
        aliases = []
        try:
            for movie in _tmdb_get("search/movie", query=query, include_adult="false").get("results", [])[:3]:
                if _result_matches(movie.get("title", ""), query):
                    for name in (movie.get("title"), movie.get("original_title")):
                        if name:
                            aliases.append(f"{name} {str(movie.get('release_date', ''))[:4]}".strip())
        except (requests.RequestException, RuntimeError, ValueError):
            pass
        finally:
            with STATE_LOCK:
                if len(TITLE_ALIASES) >= 256:
                    TITLE_ALIASES.pop(next(iter(TITLE_ALIASES)))
                TITLE_ALIASES[key] = (time.time(), list(dict.fromkeys(aliases)))
                TITLE_ALIAS_INFLIGHT.discard(key)
    threading.Thread(target=enrich, daemon=True, name="title-aliases").start()
    return list(cached[1]) if cached else []


def _regional_search_queries(query: str) -> list[str]:
    """Expand local-language titles without requiring one exact spelling."""
    candidates = [query, *SEARCH_ALIASES.get(_search_key(query), [])]
    key = _search_key(query)
    spelling_swaps = {
        "sapta": "saptha",
        "sagaradaache": "saagaradaache",
        "saagaradaache": "sagaradaache",
        "ello": "yello",
        "yello": "ello",
    }
    words = key.split()
    for old, new in spelling_swaps.items():
        if old in words:
            candidates.append(" ".join(new if word == old else word for word in words))

    candidates.extend(_title_aliases(query))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_key = _search_key(candidate)
        if candidate_key and candidate_key not in seen:
            seen.add(candidate_key)
            unique.append(candidate)
    return unique[:8]




def _result_matches(title: str, query: str) -> bool:
    ignored = {"movie", "film", "the", "a", "an", "part", "side"}
    query_tokens = {
        token for token in _search_key(query).split()
        if token not in ignored and not re.fullmatch(r"19\d{2}|20\d{2}", token)
    }
    title_tokens = set(_search_key(title).split())
    if not query_tokens:
        return True
    required = 1 if len(query_tokens) == 1 else max(2, (len(query_tokens) + 1) // 2)
    return len(query_tokens & title_tokens) >= required




# General indexes happily return software, music and worse for a film query.
# These signals are unambiguous, so a title containing one is never a film.
_NOT_VIDEO = re.compile(
    r"\b(?:keygen|activator|preactivated|crack(?:ed)?\b|serial\s+key|license\s+key|"
    r"activate\s+windows|microsoft\s+office|windows\s*&\s*office|photoshop|autocad|"
    r"antivirus|driverpack|tutorial|udemy|ebook|epub|\bpdf\b|discography|"
    r"\bflac\b|\bmp3\b|320kbps|\bost\b|soundtrack\s+album|"
    r"\bxxx\b|porn|webcam|onlyfans|hevc\s+xxx)\b"
    # An executable is never the film, however convincing the name looks:
    # "Ted Lasso S04E07 1080p HEVC x265-MeGust.exe" passed the size floor at
    # 1 GB by borrowing the real group name "MeGusta" for its extension.
    # .com and .app are deliberately absent — they are real TLDs, and release
    # names legitimately carry site prefixes like "www.example.com - Title".
    r"|\.(?:exe|scr|msi|msp|bat|cmd|pif|vbs|vbe|js|jse|wsf|wsh|ps1|"
    r"jar|apk|ipa|dmg|pkg|lnk|reg|hta|cpl|dll|deb|rpm)\b",
    re.IGNORECASE,
)

# No feature film is this small; a 27 MB "movie" is a scam or an installer.
MIN_FILM_BYTES = 80 * 1024 * 1024


def _looks_like_video(result: popcorn.TorrentResult) -> bool:
    if _NOT_VIDEO.search(result.title):
        return False
    size = result.size_bytes or _parse_size_bytes(result.size)
    if size and size < MIN_FILM_BYTES:
        return False
    return True




def _group_releases(results: list[popcorn.TorrentResult], query: str) -> list[dict]:
    """Collapse per-release listings into the titles behind them."""
    groups: dict[str, dict] = {}
    for index, result in enumerate(results):
        cleaned, year = _clean_movie_title(result.title)
        key = f"{_search_key(cleaned)}|{year or ''}"
        info = {"resolution": result.resolution, "source": result.source_type,
                "codec": result.codec, "audio": result.audio, "hdr": result.hdr,
                "language": result.language, "release_group": result.release_group,
                "season_pack": result.season_pack, "season": result.season,
                "episode": result.episode, "media_type": result.media_type}
        release = {
            "id": index,
            "title": result.title,
            "size": result.size,
            "size_bytes": result.size_bytes or _parse_size_bytes(result.size),
            "seeds": result.seeds,
            "peers": result.peers,
            "source": result.source,
            "sources": result.sources,
            "score": result.score,
            **info,
        }
        group = groups.get(key)
        if not group:
            groups[key] = {
                "key": key,
                "title": cleaned or result.title,
                "year": year,
                "releases": [release],
            }
        else:
            group["releases"].append(release)

    ordered = []
    for group in groups.values():
        releases = sorted(
            group["releases"],
            key=lambda item: item["score"],
            reverse=True,
        )
        best = releases[0]
        qualities = []
        for release in releases:
            if release["resolution"] and release["resolution"] not in qualities:
                qualities.append(release["resolution"])
        ordered.append({
            **group,
            "releases": releases,
            "release_count": len(releases),
            "qualities": qualities,
            "top_seeds": best["seeds"],
            "size": releases[0]["size"],
            "size_bytes": releases[0]["size_bytes"],
            "best_id": best["id"],
            "best_score": best["score"],
        })

    # Use the same identity-aware score for title groups and release selection.
    query_key = _search_key(query)
    ordered.sort(key=lambda group: (
        0 if query_key and query_key in _search_key(group["title"]) else 1,
        -group["best_score"],
    ))
    return ordered


def _parse_size_bytes(size: str) -> int:
    match = re.match(r"\s*([\d.]+)\s*(TB|GB|MB|KB|B)", str(size), re.IGNORECASE)
    if not match:
        return 0
    scale = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
    return int(float(match.group(1)) * scale[match.group(2).upper()])


def _tmdb_poster(movie: dict) -> str | None:
    provider_ids = movie.get("ProviderIds") or {}
    if provider_ids.get("Tmdb"):
        details = _tmdb_get(f"movie/{provider_ids['Tmdb']}")
        poster_path = details.get("poster_path")
    else:
        title, year = _clean_movie_title(movie.get("Name", ""))
        params = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = year
        results = _tmdb_get("search/movie", **params).get("results", [])
        poster_path = next((item.get("poster_path") for item in results if item.get("poster_path")), None)
    return f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None


@app.get("/api/library/movies")
@admin_required
def library_movies():
    try:
        response = _jellyfin_request(
            "GET", "Items",
            params={"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path,ProviderIds,ImageTags", "Limit": 10000},
        )
        response.raise_for_status()
        return jsonify({"movies": [{
            "id": item["Id"],
            "name": item["Name"],
            "year": item.get("ProductionYear"),
            "has_poster": bool(item.get("ImageTags", {}).get("Primary")),
        } for item in response.json().get("Items", [])]})
    except (requests.RequestException, KeyError, ValueError):
        return _error("Could not load the movie library.", 502)


def _parse_jellyfin_time(value):
    """Jellyfin timestamps carry more fractional digits than fromisoformat
    accepts on Python 3.10, so trim them before parsing."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    text = re.sub(r"\.(\d{6})\d+", r".\1", text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _sessions_playing(item_id: str) -> list[dict]:
    """Sessions genuinely playing this item right now.

    Jellyfin keeps a session's NowPlayingItem long after the client stops
    reporting — a closed tab, a phone that slept — and that entry can linger
    for hours. Treating it as live would block deleting a file nobody is
    watching, so only a recent check-in counts.

    LastPlaybackCheckIn is the only timestamp that proves playback: Jellyfin
    also exposes LastActivityDate, but that one moves whenever the member
    merely browses the site, so a stale NowPlayingItem plus everyday browsing
    would look live forever. A session with no check-in at all has never
    reported playback, so it does not block either.
    """
    try:
        response = _jellyfin_request("GET", "Sessions")
        response.raise_for_status()
        sessions = response.json()
    except (requests.RequestException, ValueError):
        return []

    now = datetime.now(timezone.utc)
    live = []
    for entry in sessions:
        playing = entry.get("NowPlayingItem") or {}
        if (playing.get("Id") or "").lower() != item_id.lower():
            continue
        checked_in = _parse_jellyfin_time(entry.get("LastPlaybackCheckIn"))
        if not checked_in or (now - checked_in).total_seconds() > PLAYBACK_GRACE_SECONDS:
            continue
        live.append(entry)
    return live


@app.delete("/api/library/movies/<item_id>")
@admin_required
def delete_movie(item_id: str):
    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", item_id):
        return _error("Invalid movie.")
    try:
        live = _sessions_playing(item_id)
        if live:
            where = ", ".join(sorted({
                (entry.get("DeviceName") or entry.get("Client") or "another device")
                for entry in live
            }))
            return _error(
                f"Still playing on {where}. Stop playback there, then delete.",
                409,
            )
        # `Items/{id}` on its own is rejected by Jellyfin 12; the ids query is
        # the form that works without a user context.
        response = _jellyfin_request("GET", "Items", params={"Ids": item_id, "Fields": "Path"})
        response.raise_for_status()
        entries = response.json().get("Items", [])
        if not entries:
            return _error("That title is no longer in your library.", 404)
        media_path = Path(entries[0].get("Path", ""))
        try:
            relative_path = media_path.relative_to(LIBRARY_PATH)
        except ValueError:
            return _error("This movie is outside the managed library path.", 409)
        if not relative_path.parts or ".." in relative_path.parts:
            return _error("The movie path is unsafe to delete.", 409)
        remote_target = relative_path
        command = "deletefile"
        # Torrent downloads commonly create a dedicated release directory.
        # Remove that directory (including subtitles/artwork) but never purge
        # the library root, where unrelated single-file movies can live.
        if len(relative_path.parts) > 1:
            remote_target = Path(relative_path.parts[0])
            command = "purge"
        deletion = subprocess.run(
            [
                "rclone", command, f"{LIBRARY_REMOTE}/{remote_target.as_posix()}",
                "--config=/home/amal/.config/rclone/rclone.conf",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        deletion_error = deletion.stderr.strip()
        already_absent = "not found" in deletion_error.casefold()
        if deletion.returncode != 0 and not already_absent:
            app.logger.error("rclone %s failed: %s", command, deletion.stderr.strip())
            return _error("The remote media file could not be deleted.", 502)
        _refresh_library_mount()
        refresh = _jellyfin_request("POST", "Library/Refresh")
        refresh.raise_for_status()
        return ("", 204)
    except (requests.RequestException, subprocess.SubprocessError, KeyError, ValueError):
        return _error("Could not delete that movie.", 502)


@app.post("/api/library/posters/repair")
@admin_required
def repair_posters():
    if not TMDB_API_KEY:
        return _error("Add POPCORN_TMDB_API_KEY to /etc/popcorn.env first.", 409)
    repaired, missing = 0, []
    try:
        response = _jellyfin_request(
            "GET", "Items",
            params={"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "ProviderIds,ImageTags", "Limit": 10000},
        )
        response.raise_for_status()
        movies = response.json().get("Items", [])
        for movie in movies:
            if movie.get("ImageTags", {}).get("Primary"):
                continue
            poster_url = _tmdb_poster(movie)
            if not poster_url:
                missing.append(movie.get("Name", "Unknown"))
                continue
            image_response = _jellyfin_request(
                "POST", f"Items/{movie['Id']}/RemoteImages/Download",
                params={"type": "Primary", "imageUrl": poster_url},
            )
            image_response.raise_for_status()
            repaired += 1
        return jsonify({"repaired": repaired, "unmatched": missing})
    except RuntimeError as exc:
        return _error(str(exc), 409)
    except (requests.RequestException, KeyError, ValueError):
        return _error("Poster repair failed. Check the TMDB key and try again.", 502)


@app.get("/api/auth/check")
def auth_check():
    return ("", 204) if _signed_in() else ("", 401)


@app.post("/api/search")
@login_required
def search():
    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query", "")).strip()
    settings = _load_settings()
    source = str(payload.get("source", settings["default_source"]))

    if len(query) < 2:
        return _error("Enter at least two characters to search.")
    if len(query) > 120:
        return _error("Search query is too long.")
    if source not in (*popcorn.source_service.providers, "all"):
        return _error("Unknown search source.")

    try:
        timeout = max(5, min(int(payload.get("timeout", settings["search_timeout"])), 60))
    except (TypeError, ValueError):
        return _error("Timeout must be a number between 5 and 60.")

    cleaned, detected_year = _clean_movie_title(query)
    try:
        context = popcorn.SearchContext(
            title=cleaned or query,
            year=int(payload["year"]) if payload.get("year") else detected_year,
            media_type=payload.get("media_type"),
            season=int(payload["season"]) if payload.get("season") is not None else None,
            episode=int(payload["episode"]) if payload.get("episode") is not None else None,
            resolution=payload.get("resolution"), language=payload.get("language"),
            codec=payload.get("codec"),
            aliases=tuple(_regional_search_queries(query)[1:3]),
        )
    except (TypeError, ValueError):
        return _error("Year, season and episode must be numbers.")
    if context.season is None:
        hints = popcorn.TorrentResult(query)
        if hints.season is not None:
            from dataclasses import replace
            context = replace(context, title=re.sub(r"\b(?:S\d{1,2}(?:\s?E\d{1,3})?|Season\s*\d{1,2})\b.*", "", query, flags=re.I).strip(),
                              media_type=hints.media_type, season=hints.season, episode=hints.episode)
    names = list(popcorn.source_service.providers) if source == "all" or "source" not in payload else [source]
    outcome = popcorn.source_service.search(context, names, deadline_seconds=timeout,
                                            preferred=source if source != "all" else None)
    results = [item for item in outcome.releases if _looks_like_video(item)]
    source_status = outcome.providers
    search_id = outcome.search_id
    with STATE_LOCK:
        _prune_searches()
        SEARCHES[search_id] = {
            "created_at": time.time(),
            "query": query,
            "results": results,
        }

    return jsonify({
        "search_id": search_id,
        "query": query,
        "count": len(results),
        "stale": outcome.stale,
        "cache_hit": outcome.cache_hit,
        "sources": source_status,
        "results": [_public_result(item, i) for i, item in enumerate(results)],
        "titles": _group_releases(results, query),
    })


_TITLE_POSTER_CACHE: dict[str, str | None] = {}


@app.get("/api/search/art")
@login_required
def search_art():
    """Poster for a title that isn't in the library yet.

    Kept off the search response so results appear as soon as the indexes
    answer, and cached because the same titles recur constantly.
    """
    title = (request.args.get("q") or "").strip()[:120]
    year = (request.args.get("y") or "").strip()[:4]
    if not title or not TMDB_API_KEY:
        return jsonify({"poster": None})
    key = f"{_search_key(title)}|{year}"
    if key not in _TITLE_POSTER_CACHE:
        url = None
        try:
            params = {"query": title, "include_adult": "false"}
            if year.isdigit():
                params["year"] = year
            candidates = _tmdb_get("search/movie", **params).get("results", [])
            path = next(
                (item.get("poster_path") for item in candidates if item.get("poster_path")), None
            )
            if path:
                url = f"https://image.tmdb.org/t/p/w342{path}"
        except (requests.RequestException, RuntimeError, ValueError):
            url = None
        if len(_TITLE_POSTER_CACHE) > 400:
            _TITLE_POSTER_CACHE.clear()
        _TITLE_POSTER_CACHE[key] = url
    return jsonify({"poster": _TITLE_POSTER_CACHE[key]})


@app.get("/api/admin/providers")
@admin_required
def provider_diagnostics():
    return jsonify({"providers": popcorn.source_service.diagnostics()})


def _set_job(job_id: str, **changes) -> None:
    with STATE_LOCK:
        previous = JOBS[job_id].copy()
        JOBS[job_id].update(changes)
        JOBS[job_id]["updated_at"] = time.time()
        snapshot = JOBS[job_id].copy()
        try:
            _persist_job(snapshot, required=True)
        except sqlite3.Error:
            # A stage checkpoint cannot be visible until it is durable.
            JOBS[job_id].clear()
            JOBS[job_id].update(previous)
            raise


def _update_job(job_id: str, *, persist: bool = True, **changes) -> None:
    """Update a job, optionally without writing it to the database.

    Clip jobs report progress far more often than a download does — every
    subtitle segment during transcription — and persisting each one would mean
    thousands of writes for a single film.
    """
    with STATE_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = time.time()
        snapshot = job.copy() if persist else None
    if snapshot:
        _persist_job(snapshot)


def _job_notes(job_id: str, message: str, limit: int = 40) -> None:
    """Keep a short log on the job, for the diagnostics disclosure."""
    with STATE_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        notes = job.setdefault("notes", [])
        notes.append(message)
        del notes[:-limit]


def _job_cancel_requested(job_id: str) -> bool:
    with STATE_LOCK:
        return bool(JOBS.get(job_id, {}).get("cancel_requested"))


def _register_job_process(job_id: str, process: subprocess.Popen) -> None:
    with STATE_LOCK:
        JOB_PROCESSES[job_id] = process
        cancel_now = bool(JOBS.get(job_id, {}).get("cancel_requested"))
    if cancel_now and process.poll() is None:
        try:
            if isinstance(process, Transfer):
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _unregister_job_process(job_id: str, process: subprocess.Popen) -> None:
    with STATE_LOCK:
        if JOB_PROCESSES.get(job_id) is process:
            JOB_PROCESSES.pop(job_id, None)


def _remove_partial_download(download_dir: str | None) -> None:
    if download_dir:
        path = Path(download_dir).resolve()
        if path.parent == ROOT and path.name.startswith("popcorn-web-"):
            shutil.rmtree(path, ignore_errors=True)


def _server_jellyfin_token(fallback_token: str | None = None) -> str | None:
    return JELLYFIN_API_KEY or fallback_token


def _refresh_library(fallback_token: str | None = None) -> bool:
    """Refresh using the server API key, with short retries for mount latency."""
    token = _server_jellyfin_token(fallback_token)
    if not token:
        return False
    for attempt in range(3):
        try:
            response = requests.post(
                f"{JELLYFIN_URL}/Library/Refresh",
                headers=_jellyfin_headers(token, ""),
                timeout=12,
            )
            response.raise_for_status()
            return True
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
    return False


def _find_library_item(
    media_paths: list[str], title: str, fallback_token: str | None = None
) -> str | None:
    """Return the Jellyfin movie matching an uploaded media path or title."""
    token = _server_jellyfin_token(fallback_token)
    if not token:
        return None
    try:
        response = requests.get(
            f"{JELLYFIN_URL}/Items",
            headers=_jellyfin_headers(token, ""),
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode,Series",
                "Fields": "Path,SeriesId",
                "Limit": 10000,
            },
            timeout=15,
        )
        response.raise_for_status()
        movies = response.json().get("Items", [])
    except (requests.RequestException, KeyError, ValueError):
        return None

    expected_paths = {str(Path(path)) for path in media_paths}
    for movie in movies:
        if str(Path(movie.get("Path", ""))) in expected_paths:
            return movie.get("SeriesId") or movie.get("Id")

    # Older jobs do not have captured file paths. Use a conservative title
    # fallback so their history links can still become direct links.
    wanted = _search_key(_clean_movie_title(title)[0])
    if wanted:
        matches = [
            movie for movie in movies
            if wanted == _search_key(_clean_movie_title(movie.get("Name", ""))[0])
        ]
        if len(matches) == 1:
            return matches[0].get("Id")
    return None


def _wait_for_library_item(
    media_paths: list[str], title: str, token: str | None
) -> str | None:
    for attempt in range(24):
        item_id = _find_library_item(media_paths, title, token)
        if item_id:
            return item_id
        if attempt < 23:
            time.sleep(5)
    return None


def _trigger_trickplay(token: str | None = None, force: bool = False) -> tuple[bool, str]:
    """Start Jellyfin's preview-frame task at most once per day."""
    token = _server_jellyfin_token(token)
    if not token:
        return False, "No Jellyfin server credential is configured."
    if not force and time.time() - _meta_timestamp("trickplay_triggered_at") < 86400:
        return True, "Preview generation was already scheduled today."
    try:
        response = requests.get(
            f"{JELLYFIN_URL}/ScheduledTasks", headers=_jellyfin_headers(token, ""), timeout=8
        )
        response.raise_for_status()
        task = next(
            (
                item for item in response.json()
                if item.get("Key") == "RefreshTrickplayImages"
                or item.get("Name", "").lower().startswith("generate trickplay")
            ),
            None,
        )
        if not task:
            return False, "This Jellyfin version has no trickplay task."
        if str(task.get("State", "")).lower() != "running":
            response = requests.post(
                f"{JELLYFIN_URL}/ScheduledTasks/Running/{task['Id']}",
                headers=_jellyfin_headers(token, ""),
                timeout=8,
            )
            response.raise_for_status()
        _set_meta_timestamp("trickplay_triggered_at")
        return True, "Preview-frame generation is running in the background."
    except (requests.RequestException, KeyError, ValueError):
        return False, "Could not start preview-frame generation."


def _job_control(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job ID")
    path = ROOT / "data" / "job-controls" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _owned_download_dir(value) -> Path | None:
    if not value:
        return None
    path = Path(value).resolve()
    if path.parent != ROOT or not path.name.startswith("popcorn-web-"):
        raise RuntimeError("The saved download directory is outside Popcorn's workspace.")
    return path



def _prepare_tv_import(job_id, result, download_dir):
    saved = JOBS[job_id]
    videos = [p for p in download_dir.rglob("*") if p.is_file()
              and p.suffix.lower() in VIDEO_EXTENSIONS and not re.search(r"\bsample\b", p.stem, re.I)]
    tv = saved.get("media_kind") == "tv" or result.media_type in {"tv", "episode", "season"} or media_library.looks_like_tv(result.title, videos)
    if not tv:
        return None
    if saved.get("media_kind") == "movie":
        raise media_library.IdentificationRequired("These files contain TV episode numbers. Choose their show before importing.")
    _set_job(job_id, status="identifying", message="Matching the show and organizing its episodes…", media_kind="tv")
    show = saved.get("show_metadata") or media_library.resolve_show(
        saved.get("show_query") or result.title, _tmdb_get, saved.get("show_tmdb_id"))
    _set_job(job_id, show_metadata=show, show_tmdb_id=show["id"])
    seasons = {}
    for season in show.get("seasons", []):
        number = season["season_number"]
        # Fetch only seasons represented by the files, plus specials when present.
        found = [media_library.coordinates(str(p), saved.get("tv_season")) for p in videos]
        wanted = {c[0] for c in found if c}
        wanted.update(c[0] for c in saved.get("episode_overrides", {}).values())
        if saved.get("tv_season") is not None:
            wanted.add(saved["tv_season"])
        if result.season is not None:
            wanted.add(result.season)
        if any(media_library.SPECIAL.search(p.stem.replace("_", " ")) for p in videos):
            wanted.add(0)
        if not wanted:
            wanted.add(saved.get("tv_season") or result.season or 1)
        if number in wanted:
            seasons[number] = _tmdb_get(f"tv/{show['id']}/season/{number}")
    overrides = dict(saved.get("episode_overrides", {}))
    if saved.get("tv_season") is not None:
        for path in videos:
            coord = media_library.coordinates(str(path), saved["tv_season"])
            if coord:
                overrides.setdefault(path.name, list(coord))
    version = " ".join(filter(None, [result.resolution, result.codec, (result.infohash or job_id)[:12]]))
    plan = media_library.episode_plan(videos, result.title, show, seasons, version, overrides)
    stage = _job_control(job_id) / "library-stage"
    folder = media_library.stage_tv(download_dir, stage, plan, show)
    # Local images ensure the read-only library mount does not depend on Jellyfin's DNS route.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(media_library.save_artwork, target, url, ROOT / "data" / "media-cache")
                   for target, url in media_library.artwork_tasks(folder, plan, show)]
        for future in futures:
            future.result()
    _set_job(job_id, destination=SHOWS_REMOTE, media_kind="tv", display_title=show["name"],
             import_plan=plan, media_paths=[str(SHOWS_PATH / e["relative"]) for e in plan])
    return stage


def _fetch_download_subtitles(job_id, result, upload_source):
    settings = _load_settings()
    saved = JOBS[job_id]
    language = settings["subtitle_language"]
    if saved.get("subtitles_checked_language") == language:
        return
    if not settings["subtitle_auto_download"]:
        _set_job(job_id, subtitles_summary={"status": "disabled"})
        return
    if not subtitle_downloads.provider_status(ROOT)["connected"]:
        _set_job(job_id, subtitles_summary={"status": "not_configured"})
        return
    _set_job(job_id, status="subtitles", message="Looking for subtitles that match your video…")
    plan = {e["relative"]: e for e in saved.get("import_plan", [])}
    files = []
    for path in upload_source.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS or re.search(r"\bsample\b", path.stem, re.I):
            continue
        entry = plan.get(str(path.relative_to(upload_source)))
        if entry:
            identity = {"kind": "episode", "title": saved["show_metadata"]["name"],
                        "show_tmdb_id": saved["show_tmdb_id"], "season": entry["season"],
                        "episode": entry["episode"], "end_episode": entry.get("end_episode")}
            release = Path(entry["source"]).stem
        else:
            title, year = _clean_movie_title(result.title)
            identity = {"kind": "movie", "title": title, "year": year or result.year}
            release = path.stem if len(path.stem) > len(title) else result.title
        files.append({"path": path, "identity": identity, "release": release})
    def check_cancelled():
        if _job_cancel_requested(job_id):
            raise DownloadCancelled()
    summary = subtitle_downloads.fetch_batch(ROOT, files, language, check_cancelled,
        lambda index, total: _set_job(job_id, message=f"Finding subtitles for video {index} of {total}…"))
    summary.pop("files", None)
    fields = {"subtitles_summary": summary}
    if summary["status"] == "complete":
        fields["subtitles_checked_language"] = language
    _set_job(job_id, **fields)


def _run_download(
    job_id: str, result: popcorn.TorrentResult, upload_to: str, jellyfin_token: str | None
) -> None:
    control = _job_control(job_id)
    # The service uses one worker. This lease also protects recovery during
    # overlapping gunicorn worker replacement and manual retries.
    with (control / "worker.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            saved = JOBS[job_id]
            download_dir = _owned_download_dir(saved.get("download_dir"))
            if _job_cancel_requested(job_id):
                if download_dir and download_dir.exists():
                    stop_transfer(download_dir)
                upload_control = control / "upload"
                if upload_control.exists():
                    stop_transfer(upload_control)
                raise DownloadCancelled()
            if not saved.get("download_complete"):
                if download_dir is None:
                    download_dir = Path(tempfile.mkdtemp(prefix="popcorn-web-", dir=ROOT))
                    _set_job(job_id, download_dir=str(download_dir))
                _set_job(job_id, status="resolving", message="Resolving download source…")
                magnet = saved.get("resolved_magnet") or popcorn.magnet_for_result(result)
                if not magnet:
                    raise RuntimeError("The source did not provide a valid magnet.")
                _set_job(job_id, resolved_magnet=magnet, status="downloading",
                         message="Downloading (resuming saved pieces when available)…")
                command = ["aria2c", *popcorn.ARIA2_FLAGS, "--summary-interval=1",
                           f"--dht-file-path={control / 'dht.dat'}",
                           f"--dht-file-path6={control / 'dht6.dat'}",
                           f"--dir={download_dir}", magnet]
                transfer = Transfer(download_dir, command)
                _register_job_process(job_id, transfer)
                saved_at = 0
                while transfer.poll() is None:
                    if _job_cancel_requested(job_id):
                        transfer.terminate()
                        transfer.wait()
                        raise DownloadCancelled()
                    matches = list(PROGRESS_PATTERN.finditer(transfer.progress()))
                    if matches:
                        values = matches[-1].groupdict()
                        now = time.time()
                        persist = now - saved_at >= 5
                        _update_job(job_id, persist=persist, progress=int(values["pct"]),
                                    speed=f"{values['speed']}/s", eta=values.get("eta") or "Calculating…",
                                    downloaded=values["done"], total=values["total"])
                        if persist:
                            saved_at = now
                    time.sleep(0.5)
                _unregister_job_process(job_id, transfer)
                code = transfer.wait()
                if _job_cancel_requested(job_id):
                    raise DownloadCancelled()
                if code != 0:
                    raise RuntimeError(f"The transfer stopped (aria2 status {code}). Saved pieces will be retried.")
                _set_job(job_id, download_complete=True, retry_count=0, progress=100, speed="—", eta="—")

            upload_source = download_dir
            normalized_upload = upload_to.rstrip("/")
            managed = normalized_upload in {LIBRARY_REMOTE, SHOWS_REMOTE}
            if managed and not saved.get("upload_complete"):
                if download_dir is None or not download_dir.exists():
                    raise RuntimeError("The completed local files are missing. Restore them before retrying upload.")
                stage = _prepare_tv_import(job_id, result, download_dir)
                if stage:
                    upload_source = stage
                    upload_to = SHOWS_REMOTE
                    normalized_upload = upload_to
            if not saved.get("upload_complete") and upload_source and upload_source.exists():
                _fetch_download_subtitles(job_id, result, upload_source)
            library_destination = bool(upload_to) and any(
                normalized_upload == remote or normalized_upload.startswith(f"{remote}/")
                for remote in (LIBRARY_REMOTE, SHOWS_REMOTE))
            media_paths = JOBS[job_id].get("media_paths", [])
            if not saved.get("upload_complete"):
                if upload_to:
                    if download_dir is None or not download_dir.exists():
                        raise RuntimeError("The completed local files are missing. Restore them before retrying upload.")
                    if library_destination and not media_paths:
                        base = LIBRARY_PATH / normalized_upload.removeprefix(LIBRARY_REMOTE).lstrip("/")
                        media_paths = [str(base / path.relative_to(download_dir))
                                       for path in download_dir.rglob("*")
                                       if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]
                        _set_job(job_id, media_paths=media_paths)
                    _set_job(job_id, status="uploading", message="Moving the finished files to your library…")
                    upload_control = control / "upload"
                    upload_control.mkdir(exist_ok=True)
                    # Copy first, commit the remote-success checkpoint, then
                    # remove local files. Retried copies skip matching files.
                    upload = Transfer(upload_control, [
                        "rclone", "copy", str(upload_source), upload_to,
                        "--exclude=.transfer*", "--exclude=*.aria2", "--exclude=*.torrent",
                        "--transfers=4", "--drive-chunk-size=32M", "--retries=3",
                        "--low-level-retries=5", "--contimeout=10s", "--timeout=2m"])
                    _register_job_process(job_id, upload)
                    while upload.poll() is None:
                        if _job_cancel_requested(job_id):
                            upload.terminate()
                            upload.wait()
                            raise DownloadCancelled()
                        time.sleep(0.5)
                    code = upload.wait()
                    _unregister_job_process(job_id, upload)
                    if _job_cancel_requested(job_id):
                        raise DownloadCancelled()
                    if code != 0:
                        raise RuntimeError(f"Upload stopped (rclone status {code}). Finished local files are kept.")
                _set_job(job_id, upload_complete=True, retry_count=0)
            if upload_to and download_dir and download_dir.exists():
                # This deletion is safe only after durable remote success.
                _remove_partial_download(str(download_dir))
                _set_job(job_id, download_dir=None)
            stage = control / "library-stage"
            if upload_to and JOBS[job_id].get("upload_complete") and stage.exists():
                shutil.rmtree(stage)
            if library_destination:
                _set_job(job_id, status="indexing", progress=100, speed="—",
                         eta="Waiting for Jellyfin…", message="Adding the title to your library…")
                _refresh_library_mount()
                item_id = _find_library_item(media_paths, result.title, jellyfin_token)
                if not item_id:
                    if not _refresh_library(jellyfin_token):
                        raise RuntimeError("The media server is unavailable. Library indexing will retry.")
                    item_id = _wait_for_library_item(media_paths, result.title, jellyfin_token)
                if not item_id:
                    raise RuntimeError("The media server is still indexing. This stage will retry.")
                _set_job(job_id, library_item_id=item_id)
                preview_timer = threading.Timer(90, _trigger_trickplay, args=(jellyfin_token, True))
                preview_timer.daemon = True
                preview_timer.start()
            stage = control / "library-stage"
            if stage.exists():
                shutil.rmtree(stage)
            _set_job(job_id, status="complete", progress=100, speed="—", eta="—",
                     message="Ready to watch." if library_destination else "Download finished.",
                     destination=upload_to or str(download_dir), retry_at=None, recoverable=False)
        except media_library.IdentificationRequired as exc:
            _set_job(job_id, status="needs_identification", media_kind="tv", retry_at=None, recoverable=False,
                     speed="—", eta="—", message=str(exc) + " Finished files are kept locally.",
                     identification_files=[p.name for p in download_dir.rglob("*")
                                           if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS])
        except DownloadCancelled:
            saved = JOBS[job_id]
            # Stop may discard partial downloads, never a completed local copy.
            if not saved.get("download_complete"):
                _remove_partial_download(saved.get("download_dir"))
                _set_job(job_id, download_dir=None)
            _set_job(job_id, status="cancelled", speed="—", eta="—", retry_at=None,
                     message="Stopped. Finished local files were kept." if saved.get("download_complete")
                     else "Stopped. Partial files were removed.", recoverable=False)
        except Exception as exc:
            # Do not log exception text: upstream URLs/commands can contain secrets.
            app.logger.error("download_job_failed job_id=%s stage=%s error_type=%s",
                             job_id, JOBS[job_id].get("status"), type(exc).__name__)
            saved = JOBS[job_id]
            attempts = saved.get("retry_count", 0) + 1
            delay = min(JOB_RETRY_MAX, JOB_RETRY_BASE * 2 ** min(attempts - 1, 12))
            stage = "indexing" if saved.get("upload_complete") else "uploading" if saved.get("download_complete") else "downloading" if saved.get("resolved_magnet") else "resolving"
            message = str(exc) if isinstance(exc, RuntimeError) else "This stage stopped temporarily. Saved files are kept."
            _set_job(job_id, status=stage, retry_count=attempts, retry_at=time.time() + delay,
                     recoverable=True, speed="—", eta="—", message=f"{message} Retrying in {int(delay)} seconds.")
        finally:
            with STATE_LOCK:
                JOB_PROCESSES.pop(job_id, None)


def _start_download_job(job_id, token=None):
    with STATE_LOCK:
        saved = JOBS[job_id]
        if job_id in JOB_WORKERS or len(JOB_WORKERS) >= JOB_MAX_WORKERS:
            return False
        if saved.get("kind") == "clips" or saved.get("status") in TERMINAL_JOB_STATES:
            return False
        result = popcorn.TorrentResult(**saved["release"]) if saved.get("release") else popcorn.TorrentResult(saved["title"])
        upload_to = saved.get("destination", "")
        JOB_WORKERS.add(job_id)
    def run():
        try:
            _run_download(job_id, result, upload_to, token)
        finally:
            with STATE_LOCK:
                JOB_WORKERS.discard(job_id)
    threading.Thread(target=run, daemon=True, name=f"download-{job_id[:8]}").start()
    return True


def _recover_download_jobs():
    while True:
        try:
            with STATE_LOCK:
                ready = [j["id"] for j in JOBS.values()
                         if j.get("kind") != "clips" and j.get("status") in ACTIVE_JOB_STATES
                         and (j.get("retry_at") or 0) <= time.time()]
            for job_id in ready:
                _start_download_job(job_id)
        except Exception as exc:
            app.logger.error("job_recovery_failed error_type=%s", type(exc).__name__)
        time.sleep(5)


@app.post("/api/download")
@login_required
def download():
    payload = request.get_json(silent=True) or {}
    search_id = str(payload.get("search_id", ""))
    default_upload = _load_settings()["default_upload"]
    upload_to = str(payload.get("upload_to", default_upload)).strip() or default_upload
    # Artwork the browser already resolved, so Downloads can show what is being
    # fetched instead of a bare release string.
    artwork = str(payload.get("poster", ""))[:400]
    if artwork and not (artwork.startswith("http") or artwork.startswith("/")):
        artwork = ""
    try:
        result_id = int(payload.get("result_id"))
        identity = _tv_identity_payload(payload)
    except (TypeError, ValueError):
        return _error("Choose a search result first.")

    with STATE_LOCK:
        saved_search = SEARCHES.get(search_id)
        if not saved_search:
            return _error("This search has expired. Search again.", 404)
        results = saved_search["results"]
        if result_id < 0 or result_id >= len(results):
            return _error("That result no longer exists.", 404)
        result = results[result_id]
        job_id = uuid.uuid4().hex
        now = time.time()
        JOBS[job_id] = {
            "id": job_id,
            "owner_user_id": (session.get("jellyfin") or {}).get("user_id"),
            "title": result.title,
            "display_title": _clean_movie_title(result.title)[0] or result.title,
            "poster": artwork or None,
            "source": result.source,
            "size": result.size,
            "destination": upload_to,
            **identity,
            "release": asdict(result),
            "download_complete": False,
            "upload_complete": False,
            "status": "queued",
            "message": "Download queued…",
            "progress": 0,
            "speed": "Waiting…",
            "eta": "—",
            "created_at": now,
            "updated_at": now,
        }
        created_job = JOBS[job_id].copy()

    _persist_job(created_job, required=True)
    _start_download_job(job_id, (session.get("jellyfin") or {}).get("token"))
    return jsonify(_public_job(JOBS[job_id])), 202


def _tv_identity_payload(payload):
    kind = str(payload.get("media_kind", "auto"))
    if kind not in {"auto", "movie", "tv"}:
        raise ValueError("Unknown media type.")
    tmdb_id = int(payload["show_tmdb_id"]) if payload.get("show_tmdb_id") else None
    season = int(payload["tv_season"]) if payload.get("tv_season") not in {None, ""} else None
    if (tmdb_id is not None and tmdb_id <= 0) or (season is not None and not 0 <= season <= 99):
        raise ValueError("Invalid show or season.")
    return {"media_kind": kind, "show_tmdb_id": tmdb_id, "tv_season": season,
            "show_query": str(payload.get("show_query", "")).strip()[:200]}


@app.get("/api/tv/search")
@login_required
def search_tv_identity():
    query = media_library.show_query(request.args.get("q", ""))[:200]
    if len(query) < 2:
        return _error("Enter a TV show name.")
    try:
        shows = _tmdb_get("search/tv", query=query, include_adult="false").get("results", [])
    except RuntimeError:
        return _error("Show details are temporarily unavailable. Try again.", 502)
    return jsonify({"shows": [{"id": s["id"], "title": s["name"],
                               "year": (s.get("first_air_date") or "")[:4]} for s in shows[:12]]})


@app.post("/api/jobs/<job_id>/identify")
@login_required
def identify_download(job_id):
    connection = session.get("jellyfin") or {}
    with STATE_LOCK:
        saved = JOBS.get(job_id)
        if not saved or saved.get("owner_user_id") != connection.get("user_id"):
            return _error("Download job not found.", 404)
        if saved.get("status") != "needs_identification" or job_id in JOB_WORKERS:
            return _error("This job is not waiting for identification.", 409)
    try:
        payload = request.get_json(silent=True) or {}
        identity = _tv_identity_payload(payload)
        overrides = payload.get("episode_overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("Invalid episode mapping.")
        for name, coord in overrides.items():
            if name not in saved.get("identification_files", []) or not isinstance(coord, list) or len(coord) != 3:
                raise ValueError("Unknown episode file.")
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in coord[:2]) or not 0 <= coord[0] <= 99 or not 1 <= coord[1] <= 999 or coord[2] is not None:
                raise ValueError("Invalid episode number.")
        identity["episode_overrides"] = overrides
    except (ValueError, TypeError):
        return _error("Choose a show and a valid season.")
    identity["media_kind"] = "tv"
    _set_job(job_id, **identity, show_metadata=None, identification_files=[], status="queued", retry_at=0,
             cancel_requested=False, message="Organizing the saved TV episodes…")
    _start_download_job(job_id, connection.get("token"))
    return jsonify(_public_job(JOBS[job_id])), 202


def _public_job(job: dict) -> dict:
    """A job as the interface needs it: no server paths, and a title written
    for a person rather than an indexer."""
    payload = {key: value for key, value in job.items()
               if key not in {"download_dir", "media_paths", "work_dir", "cancel_requested", "release", "resolved_magnet", "show_metadata", "import_plan", "episode_overrides"}}
    payload["display_title"] = (job.get("show_metadata") or {}).get("name") or _display_title(job.get("title", "")) or job.get("title", "")
    if job.get("kind") == "clips":
        payload["notes"] = (job.get("notes") or [])[-6:]
    return payload


@app.get("/api/jobs/<job_id>")
@login_required
def job(job_id: str):
    with STATE_LOCK:
        current = JOBS.get(job_id)
        if not current:
            return _error("Download job not found.", 404)
        connection = session.get("jellyfin") or {}
        owner = current.get("owner_user_id")
        if owner != connection.get("user_id") and not (owner is None and connection.get("is_admin")):
            return _error("Download job not found.", 404)
        return jsonify(_public_job(current))


def _terminal_states(job: dict) -> set[str]:
    """Which states end a job — download jobs and clip jobs differ."""
    return CLIP_TERMINAL_STATES if job.get("kind") == "clips" else TERMINAL_JOB_STATES


@app.post("/api/jobs/<job_id>/cancel")
@login_required
def cancel_job(job_id: str):
    with STATE_LOCK:
        current = JOBS.get(job_id)
        if not current:
            return _error("Download job not found.", 404)
        connection = session.get("jellyfin") or {}
        owner = current.get("owner_user_id")
        if owner != connection.get("user_id") and not (owner is None and connection.get("is_admin")):
            return _error("Download job not found.", 404)
        clips = current.get("kind") == "clips"
        if current.get("status") in _terminal_states(current):
            return _error(
                "This job has already finished." if clips else "This download has already finished.",
                409,
            )
        current.update({
            "cancel_requested": True,
            "status": "cancelling",
            "message": (
                "Stopping clip generation. Clips already saved are kept."
                if clips else "Stopping download and removing partial files…"
            ),
            "updated_at": time.time(),
        })
        snapshot = current.copy()
        process = JOB_PROCESSES.get(job_id)
    _persist_job(snapshot)
    if process and process.poll() is None:
        try:
            if isinstance(process, Transfer):
                # Worker polls cancellation and performs the supervised stop.
                pass
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return jsonify(_public_job(snapshot)), 202


@app.post("/api/jobs/<job_id>/retry")
@login_required
def retry_job(job_id):
    with STATE_LOCK:
        current = JOBS.get(job_id)
        connection = session.get("jellyfin") or {}
        if not current or (current.get("owner_user_id") != connection.get("user_id")
                           and not (current.get("owner_user_id") is None and connection.get("is_admin"))):
            return _error("Download job not found.", 404)
        if current.get("kind") == "clips" or not (current.get("release") or current.get("download_complete")):
            return _error("This older job cannot be resumed automatically.", 409)
        if current.get("status") in {"complete", "cancelled", "cancelling"} or job_id in JOB_WORKERS:
            return _error("This job is already running or finished.", 409)
    _set_job(job_id, status="queued", retry_at=0, cancel_requested=False, message="Resuming saved work…")
    _start_download_job(job_id, connection.get("token"))
    return jsonify(_public_job(JOBS[job_id])), 202


@app.delete("/api/jobs/<job_id>")
@login_required
def delete_job(job_id: str):
    with STATE_LOCK:
        current = JOBS.get(job_id)
        if not current:
            return _error("Download job not found.", 404)
        connection = session.get("jellyfin") or {}
        owner = current.get("owner_user_id")
        if owner != connection.get("user_id") and not (owner is None and connection.get("is_admin")):
            return _error("Download job not found.", 404)
        if current.get("status") not in _terminal_states(current):
            return _error("Stop this download before removing it from history.", 409)
        JOBS.pop(job_id, None)
    with _database() as connection:
        connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return ("", 204)


@app.get("/api/jobs/<job_id>/watch")
@login_required
def job_watch(job_id: str):
    with STATE_LOCK:
        current = JOBS.get(job_id)
        connection = session.get("jellyfin") or {}
        if not current:
            return _error("Download job not found.", 404)
        owner = current.get("owner_user_id")
        if owner != connection.get("user_id") and not (owner is None and connection.get("is_admin")):
            return _error("Download job not found.", 404)
        if current.get("status") != "complete":
            return _error("This movie is not ready to watch yet.", 409)
        item_id = current.get("library_item_id")
        media_paths = current.get("media_paths") or []
        title = current.get("title", "")
    if not item_id:
        item_id = _find_library_item(media_paths, title, connection.get("token"))
        if item_id:
            _set_job(job_id, library_item_id=item_id)
    if not item_id:
        return _error("Jellyfin is still adding this movie. Try again shortly.", 409)
    return jsonify({"url": url_for("watch", item=item_id)})


@app.get("/api/jobs")
@login_required
def jobs():
    with STATE_LOCK:
        connection = session.get("jellyfin") or {}
        user_id = connection.get("user_id")
        # Downloads and clip jobs share one registry but not one screen: the
        # Downloads page and its tab badge only ever want downloads.
        kind = request.args.get("kind", "download")
        items = [
            item for item in JOBS.values()
            if (item.get("kind") == "clips") == (kind == "clips")
            and (item.get("owner_user_id") == user_id
                 or (item.get("owner_user_id") is None and connection.get("is_admin")))
        ]
        scope = request.args.get("scope", "all")
        active_states = CLIP_ACTIVE_STATES if kind == "clips" else ACTIVE_JOB_STATES
        terminal_states = CLIP_TERMINAL_STATES if kind == "clips" else TERMINAL_JOB_STATES
        if scope == "active":
            items = [item for item in items if item.get("status") in active_states]
        elif scope == "history":
            items = [item for item in items if item.get("status") in terminal_states]
        elif scope != "all":
            return _error("Unknown job scope.")
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return jsonify({"jobs": [_public_job(item) for item in items[:100]]})


@app.post("/api/library/trickplay")
@login_required
def library_trickplay():
    token = (session.get("jellyfin") or {}).get("token")
    if not token:
        return _error("The watch library is not connected.", 409)
    ok, message = _trigger_trickplay(token)
    return jsonify({"ok": ok, "message": message}), 200 if ok else 502


# ═══════════════════════════════════════════════════════════════════════════
# Library layer — a thin, opinionated proxy over Jellyfin.
#
# The viewer-facing app never talks to Jellyfin directly: it reads the shapes
# built here, which are stable regardless of Jellyfin version or how messy the
# underlying metadata is. Images and media are streamed through this process so
# that authentication stays in one place and Jellyfin credentials never reach
# the browser.
# ═══════════════════════════════════════════════════════════════════════════

TICKS_PER_SECOND = 10_000_000
IMAGE_KINDS = {"poster": "Primary", "backdrop": "Backdrop", "logo": "Logo", "thumb": "Thumb"}
# Listed smallest first so srcset can offer a real ladder without upscaling.
IMAGE_WIDTHS = {
    "poster": (180, 300, 420),
    "backdrop": (900, 1600, 2400),
    "logo": (300, 600, 900),
    "thumb": (320, 480, 720),
}
BROWSE_SORTS = {
    "added": ("DateCreated", "Descending"),
    "title": ("SortName", "Ascending"),
    "year": ("ProductionYear", "Descending"),
    "rating": ("CommunityRating", "Descending"),
    "runtime": ("Runtime", "Descending"),
}


def _library_connection() -> dict:
    return session.get("jellyfin") or {}


def _library_request(method: str, path: str, **kwargs) -> requests.Response:
    """Jellyfin call on behalf of the signed-in member.

    The member's own device token is used whenever there is one: Jellyfin
    attributes watch state and playback sessions to whichever token made the
    call, so using the server API key here would silently record everything
    against the server instead of the viewer. The API key is only a fallback
    for a legacy login that never negotiated a device token.
    """
    connection = _library_connection()
    token = connection.get("token") or _server_jellyfin_token()
    if not token:
        raise requests.RequestException("The watch library is not connected.")
    device_id = connection.get("device_id", "") if connection.get("token") else ""
    headers = kwargs.pop("headers", {})
    return requests.request(
        method,
        f"{JELLYFIN_URL}/{path.lstrip('/')}",
        headers={**_jellyfin_headers(token, device_id), **headers},
        timeout=kwargs.pop("timeout", 15),
        **kwargs,
    )


def _library_json(path: str, **params) -> dict:
    response = _library_request("GET", path, params=params)
    response.raise_for_status()
    return response.json()


class LibraryUnavailable(Exception):
    """Jellyfin is unreachable or rejected the request."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


def _library_guard(view):
    """Turn Jellyfin failures into one friendly, actionable error shape."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except requests.Timeout as exc:
            return _error(
                "Your media server is taking too long to respond.", 504, detail=str(exc)
            )
        except requests.ConnectionError as exc:
            return _error("Couldn't reach your media server.", 503, detail=str(exc))
        except requests.HTTPError as exc:
            return _error(
                "Your media server couldn't complete that request.", 502, detail=str(exc)
            )
        except requests.RequestException as exc:
            return _error("The watch library isn't connected.", 503, detail=str(exc))
        except (KeyError, ValueError, TypeError) as exc:
            return _error(
                "Your media server returned something unexpected.", 502,
                detail=f"{type(exc).__name__}: {exc}",
            )
    return wrapped


def _minutes(ticks) -> int | None:
    try:
        return round(int(ticks) / TICKS_PER_SECOND / 60)
    except (TypeError, ValueError):
        return None


def _media_url(path: str, **params) -> str:
    """Build an in-app URL for the media proxy.

    `url_for` can't be used here: Flask turns an unknown keyword into another
    query parameter rather than encoding a query string.
    """
    query = urlencode({key: value for key, value in params.items() if value is not None})
    return f"/media/{path.lstrip('/')}" + (f"?{query}" if query else "")


def _resolution_label(video: dict) -> str | None:
    """A resolution people recognise.

    Jellyfin reports the encoded height, which for a 2.40:1 film is 800 — not a
    number anyone looks for. Prefer the label Jellyfin already computed, and
    otherwise bucket by width.
    """
    match = re.search(r"\b(4K|8K|\d{3,4}p)\b", video.get("DisplayTitle") or "")
    if match:
        return match.group(1)
    width = video.get("Width")
    if not width:
        return None
    if width >= 3400:
        return "4K"
    if width >= 1800:
        return "1080p"
    if width >= 1200:
        return "720p"
    return "480p"


def _art(item: dict, kind: str) -> dict | None:
    """Build responsive art URLs for one image kind, or None when absent."""
    image_type = IMAGE_KINDS[kind]
    item_id, tags = item.get("Id"), item.get("ImageTags") or {}
    parent_id = item.get("ParentBackdropItemId") or item.get("SeriesId")
    parent_tags = item.get("ParentBackdropImageTags") or []

    if kind == "backdrop":
        if item.get("BackdropImageTags"):
            tag = item["BackdropImageTags"][0]
        elif parent_id and parent_tags:
            item_id, tag = parent_id, parent_tags[0]
        else:
            return None
    elif kind == "poster":
        tag = tags.get("Primary")
    elif kind == "thumb":
        # A landscape still. Episodes carry one as their Primary image; for a
        # movie, Primary is a poster and would be badly cropped here, so only a
        # real Thumb image or the parent's backdrop will do.
        tag = tags.get("Primary") if item.get("Type") == "Episode" else tags.get("Thumb")
        if tag and item.get("Type") == "Episode":
            image_type = "Primary"
        if not tag and parent_id and parent_tags:
            item_id, tag, image_type = parent_id, parent_tags[0], "Backdrop"
    elif kind == "logo":
        tag = tags.get("Logo")
    else:
        tag = None

    if not tag or not item_id:
        return None

    def url(width: int) -> str:
        route_kind = next(key for key, value in IMAGE_KINDS.items() if value == image_type)
        return url_for("library_art", item_id=item_id, kind=route_kind, tag=tag, w=width)

    return {
        "src": url(IMAGE_WIDTHS[kind][1]),
        "srcset": ", ".join(f"{url(w)} {w}w" for w in IMAGE_WIDTHS[kind]),
        "tag": tag,
    }


# Release-site noise that Jellyfin could not parse away. Cleaning this for
# display only — the library itself is never rewritten, and the untidy
# original stays available as `library_title`.
_SITE_PREFIX = re.compile(r"^(?:www\.\S+\s*-\s*|copy of\s+)", re.IGNORECASE)
_LEADING_DOMAIN = re.compile(
    r"^[\w-]+\.(?:com|org|net|info|in|to|mx|bz|cc|me|tv|site|link|tips|xyz|club|pro|ws|se)\b[\s.\-]+",
    re.IGNORECASE,
)
_QUALITY_TAIL = re.compile(
    # Requires a separator before the token, so a title that *starts* with one
    # ("2.0", "1080") is never mistaken for release metadata.
    r"(?<=[\s._\-\[\(])(?:2160p|1440p|1080p|720p|480p|bluray|blu-ray|brrip|bdrip|webrip|web-dl|"
    r"webdl|hdrip|dvdrip|dvdscr|telesync|hdcam|x264|x265|h\.?264|h\.?265|hevc|avc|aac\d?|"
    r"ddp?\d?|ac3|dts|imax|10bit|5\.1|2\.0|7\.1)\b.*$",
    re.IGNORECASE,
)
_DUPLICATE_YEAR = re.compile(r"\((\d{4})\)\s*\(\1\)")
_EDGE_JUNK = re.compile(r"^[\s.\-_]+|[\s.\-_]+$")
# "Interstellar (2014) [" — an opening bracket left behind by a stripped tail.
_DANGLING_OPEN = re.compile(r"[\s.\-_]*[\[({]\s*[\s.\-_]*$")


def _display_title(name: str) -> str:
    """A title worth showing, without touching the library record."""
    original = (name or "").strip()
    if not original:
        return original

    cleaned = _SITE_PREFIX.sub("", original)
    cleaned = _LEADING_DOMAIN.sub("", cleaned)
    cleaned = _QUALITY_TAIL.sub("", cleaned)
    cleaned = _DUPLICATE_YEAR.sub(r"(\1)", cleaned)

    # "Ted.Lasso.S04E06.Dont.Jump" has no spaces to wrap on, so treat the
    # separators as spaces — but only for a name that is clearly a filename,
    # so a real title like "2.0" survives.
    if " " not in cleaned and cleaned.count(".") >= 2:
        cleaned = re.sub(r"[._]+", " ", cleaned)

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = _DANGLING_OPEN.sub("", cleaned)
    cleaned = _EDGE_JUNK.sub("", cleaned)

    # Never let cleanup lose a title entirely.
    if len(cleaned) < 2:
        return original
    return cleaned


def _user_state(item: dict) -> dict:
    data = item.get("UserData") or {}
    position = data.get("PlaybackPositionTicks") or 0
    runtime = item.get("RunTimeTicks") or 0
    # Treat "finished" as watched: Jellyfin leaves a few seconds of slack.
    percent = 0
    if runtime and position:
        percent = min(100, round(position / runtime * 100))
    return {
        "played": bool(data.get("Played")),
        "favorite": bool(data.get("IsFavorite")),
        "position_ticks": position,
        "position_seconds": round(position / TICKS_PER_SECOND),
        "percent": percent,
        "unplayed_count": data.get("UnplayedItemCount") or 0,
    }


def _public_item(item: dict, detail: bool = False) -> dict:
    """The one item shape every screen consumes."""
    item_type = item.get("Type", "")
    series = None
    if item.get("SeriesId") or item_type == "Episode":
        series = {"id": item.get("SeriesId"), "name": item.get("SeriesName")}
    elif item_type == "Series":
        series = {"id": item.get("Id"), "name": item.get("Name")}

    payload = {
        "id": item.get("Id"),
        "type": item_type,
        "title": _display_title(item.get("Name")) or "Untitled",
        "library_title": item.get("Name") or "Untitled",
        "original_title": item.get("OriginalTitle") or None,
        "year": item.get("ProductionYear"),
        "runtime_minutes": _minutes(item.get("RunTimeTicks")),
        "community_rating": item.get("CommunityRating"),
        "critic_rating": item.get("CriticRating"),
        "official_rating": item.get("OfficialRating") or None,
        "genres": item.get("Genres") or [],
        "overview": item.get("Overview") or None,
        "tagline": item.get("Tagline") or None,
        "premiere_date": (item.get("PremiereDate") or "")[:10] or None,
        "date_created": item.get("DateCreated"),
        "index_number": item.get("IndexNumber"),
        "parent_index_number": item.get("ParentIndexNumber"),
        "series": series if series and series.get("id") else None,
        "user": _user_state(item),
        "poster": _art(item, "poster"),
        "backdrop": _art(item, "backdrop"),
        "logo": _art(item, "logo"),
        "thumb": _art(item, "thumb"),
    }

    if detail:
        people = item.get("People") or []
        payload["cast"] = [
            {
                "name": person.get("Name"),
                "role": person.get("Role") or None,
                "type": person.get("Type"),
                "image": url_for("library_person_art", person_id=person["Id"])
                if person.get("Id") and (person.get("PrimaryImageTag") or person.get("ImageTags", {}).get("Primary"))
                else None,
            }
            for person in people
            if person.get("Type") in {"Actor", "Director", "Writer", "Producer", "GuestStar"}
        ][:24]
        payload["directors"] = [
            person.get("Name") for person in people if person.get("Type") == "Director"
        ][:4]
        payload["writers"] = [
            person.get("Name") for person in people if person.get("Type") == "Writer"
        ][:4]
        payload["studios"] = [studio.get("Name") for studio in item.get("Studios") or []][:4]
        payload["trailers"] = [
            {"url": trailer.get("Url"), "name": trailer.get("Name")}
            for trailer in item.get("RemoteTrailers") or []
            if trailer.get("Url")
        ][:2]
        payload["media"] = _media_summary(item)
        payload["versions"] = [
            {
                "id": source.get("Id"),
                "name": source.get("Name") or source.get("DisplayTitle"),
                "container": source.get("Container"),
                "size": source.get("Size"),
                "runtime_minutes": _minutes(source.get("RunTimeTicks")),
                "video": _stream_label(source, "Video"),
                "audio": _stream_label(source, "Audio"),
                "height": next(
                    (s.get("Height") for s in source.get("MediaStreams") or [] if s.get("Type") == "Video"),
                    None,
                ),
            }
            for source in item.get("MediaSources") or []
        ]
        if payload["versions"] and len(payload["versions"]) == 1:
            payload["versions"] = []

    return payload


def _stream_label(source: dict, stream_type: str) -> str | None:
    for stream in source.get("MediaStreams") or []:
        if stream.get("Type") == stream_type:
            return stream.get("DisplayTitle") or stream.get("Codec")
    return None


def _media_summary(item: dict) -> dict | None:
    sources = item.get("MediaSources") or []
    if not sources:
        return None
    source = sources[0]
    streams = source.get("MediaStreams") or []
    video = next((s for s in streams if s.get("Type") == "Video"), {})
    audio = [s for s in streams if s.get("Type") == "Audio"]
    subtitles = [s for s in streams if s.get("Type") == "Subtitle"]
    return {
        "container": (source.get("Container") or "").split(",")[0].upper() or None,
        "size": source.get("Size"),
        "bitrate": source.get("Bitrate"),
        "resolution": _resolution_label(video),
        "video_codec": (video.get("Codec") or "").upper() or None,
        "hdr": video.get("VideoRangeType") if video.get("VideoRangeType") not in (None, "SDR") else None,
        "audio": [
            {
                "codec": (stream.get("Codec") or "").upper(),
                "channels": stream.get("ChannelLayout") or None,
                "language": _language_name(stream.get("Language")),
            }
            for stream in audio
        ][:6],
        "subtitle_count": len(subtitles),
        "subtitle_languages": sorted({
            name for name in (_language_name(s.get("Language")) for s in subtitles) if name
        }),
    }


def _language_name(code: str | None) -> str | None:
    if not code or code.lower() in {"und", "unknown", "mul"}:
        return None
    return code.upper() if len(code) <= 3 else code.title()


# Jellyfin exposes format names that mean nothing to a viewer.
_FORMAT_NAMES = {
    "SUBRIP": "SRT", "SRT": "SRT", "ASS": "ASS", "SSA": "SSA",
    "WEBVTT": "VTT", "VTT": "VTT", "MOV_TEXT": "TX3G", "TTML": "TTML",
    "PGSSUB": "PGS", "DVDSUB": "VOBSUB",
}


def _track_title(stream: dict, index: int) -> str:
    """A track label a person can choose from, e.g. "English · SRT"."""
    details = []
    codec = (stream.get("Codec") or "").upper()
    if codec:
        details.append(_FORMAT_NAMES.get(codec, codec))
    if stream.get("IsHearingImpaired"):
        details.append("SDH")
    # Many sidecar files carry no language tag; the format alone is a better
    # label than a track number nobody can interpret.
    label = _language_name(stream.get("Language")) or (details[0] if details else f"Track {index}")
    if label in details:
        details = details[1:]
    title = f"{label} · {' · '.join(details)}" if details else label
    if stream.get("IsForced"):
        title += " (forced)"
    return title


def _browse_query(item_type: str, genre: str | None, sort: str) -> dict:
    field, direction = BROWSE_SORTS.get(sort, BROWSE_SORTS["added"])
    params = {
        "Recursive": "true",
        "IncludeItemTypes": item_type,
        "SortBy": field,
        "SortOrder": direction,
        "Fields": "DateCreated,Genres,Overview,CommunityRating,OfficialRating",
        "ImageTypeLimit": 1,
        "EnableImageTypes": "Primary,Backdrop,Logo,Thumb",
    }
    if genre:
        params["Genres"] = genre
    return params


@app.get("/api/home")
@login_required
@_library_guard
def library_home():
    """Everything the home screen needs, in one round trip."""
    connection = _library_connection()
    user_id = connection.get("user_id")
    if not user_id:
        return _error("The watch library is not connected.", 409)

    common = {
        "Recursive": "true",
        "Fields": "DateCreated,Genres,Overview,CommunityRating,OfficialRating,RunTimeTicks",
        "ImageTypeLimit": 1,
        "EnableImageTypes": "Primary,Backdrop,Logo,Thumb",
    }
    fields = "DateCreated,Genres,Overview,CommunityRating,OfficialRating,RunTimeTicks"

    def items(**params) -> list[dict]:
        query = {**common, "Limit": 24, **params}
        return _library_json(f"Users/{user_id}/Items", **query).get("Items", [])

    resume = _library_json(
        f"Users/{user_id}/Items/Resume",
        Recursive="true", Limit=12, MediaTypes="Video",
        Fields=fields, ImageTypeLimit=1,
        EnableImageTypes="Primary,Backdrop,Logo,Thumb",
    ).get("Items", [])
    recently_added = items(IncludeItemTypes="Movie,Series", SortBy="DateCreated", SortOrder="Descending")
    movies = items(IncludeItemTypes="Movie", SortBy="SortName", SortOrder="Ascending")
    shows = items(IncludeItemTypes="Series", SortBy="SortName", SortOrder="Ascending")
    watchlist = items(Filters="IsFavorite")
    collections = items(IncludeItemTypes="BoxSet", SortBy="SortName", SortOrder="Ascending", Limit=12)

    rows: list[dict] = []

    def add_row(key: str, title: str, entries: list[dict], **extra) -> None:
        if entries:
            rows.append({
                "key": key,
                "title": title,
                "items": [_public_item(entry) for entry in entries],
                **extra,
            })

    add_row("continue", "Continue watching", resume, kind="wide")
    add_row("recent", "Recently added", recently_added)
    add_row("watchlist", "Your watchlist", watchlist, href="/movies?filter=watchlist")

    # "Because you watched…" — rooted in the most recent thing they actually
    # played, so the row changes as their taste does.
    seed = next((entry for entry in resume if entry.get("Id")), None)
    if not seed:
        played = items(Filters="IsPlayed", SortBy="DatePlayed", SortOrder="Descending", Limit=1)
        seed = played[0] if played else None
    if seed:
        try:
            similar = _library_json(
                f"Items/{seed['Id']}/Similar",
                UserId=user_id, Limit=18, Fields=fields,
                ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Logo,Thumb",
            ).get("Items", [])
            owned = {entry.get("Id") for entry in (*movies, *shows, *recently_added)}
            similar = [entry for entry in similar if entry.get("Id") in owned]
            add_row("because", f"Because you watched {seed.get('Name')}", similar)
        except (requests.RequestException, KeyError, ValueError):
            pass

    # Recently finished downloads, resolved back to their library entries.
    completed_ids = [
        job.get("library_item_id")
        for job in sorted(JOBS.values(), key=lambda job: job.get("created_at", 0), reverse=True)
        if job.get("status") == "complete"
        and job.get("library_item_id")
        and job.get("owner_user_id") == user_id
    ][:12]
    if completed_ids:
        try:
            downloaded = _library_json(
                f"Users/{user_id}/Items", Ids=",".join(completed_ids), **common
            ).get("Items", [])
            order = {item_id: index for index, item_id in enumerate(completed_ids)}
            downloaded.sort(key=lambda entry: order.get(entry.get("Id"), 99))
            add_row("downloaded", "Recently downloaded", downloaded)
        except (requests.RequestException, KeyError, ValueError):
            pass

    add_row("collections", "Collections", collections)
    add_row("movies", "Movies", movies, href="/movies")
    add_row("shows", "TV shows", shows, href="/shows")

    # Genre rows for the three genres with the most titles behind them.
    try:
        genres = _library_json(
            "Genres", UserId=user_id, Recursive="true",
            IncludeItemTypes="Movie,Series", SortBy="SortName", Limit=40,
        ).get("Items", [])
        top = sorted(genres, key=lambda genre: genre.get("ItemCount") or 0, reverse=True)[:3]
        for genre in top:
            name = genre.get("Name")
            if not name or (genre.get("ItemCount") or 0) < 4:
                continue
            add_row(
                f"genre:{name}", name,
                items(IncludeItemTypes="Movie,Series", Genres=name, SortBy="CommunityRating", SortOrder="Descending", Limit=18),
                href=f"/movies?genre={quote(name)}",
            )
    except (requests.RequestException, KeyError, ValueError):
        pass

    # A hero needs wide art behind it. A poster stretched across the top of the
    # page looks broken, so those titles are left to the rows instead.
    hero_pool = [*resume[:2], *recently_added]
    seen: set[str] = set()
    hero = []
    for entry in hero_pool:
        if entry.get("Id") in seen or not entry.get("BackdropImageTags"):
            continue
        seen.add(entry["Id"])
        hero.append(_public_item(entry))
        if len(hero) == 5:
            break

    return jsonify({
        "hero": hero,
        "rows": rows,
        "empty": not rows,
        "library": {
            "movie_count": len(movies),
            "series_count": len(shows),
        },
    })


@app.get("/api/browse")
@login_required
@_library_guard
def library_browse():
    connection = _library_connection()
    user_id = connection.get("user_id")
    if not user_id:
        return _error("The watch library is not connected.", 409)

    item_type = request.args.get("type", "Movie")
    if item_type not in {"Movie", "Series", "BoxSet"}:
        return _error("Unknown library type.")
    genre = request.args.get("genre") or None
    sort = request.args.get("sort", "added")
    watchlist_only = request.args.get("filter") == "watchlist"
    try:
        start = max(0, int(request.args.get("start", 0)))
        limit = max(1, min(int(request.args.get("limit", 60)), 120))
    except (TypeError, ValueError):
        return _error("Invalid paging.")

    params = _browse_query(item_type, genre, sort)
    if watchlist_only:
        params["Filters"] = "IsFavorite"
    payload = _library_json(f"Users/{user_id}/Items", StartIndex=start, Limit=limit, **params)

    genres = []
    if not genre and not watchlist_only and start == 0:
        try:
            genres = [
                {"name": entry.get("Name"), "count": entry.get("ItemCount") or 0}
                for entry in _library_json(
                    "Genres", UserId=user_id, Recursive="true",
                    IncludeItemTypes=item_type, SortBy="SortName", Limit=40,
                ).get("Items", [])
                if entry.get("Name")
            ]
        except (requests.RequestException, KeyError, ValueError):
            genres = []

    return jsonify({
        "items": [_public_item(entry) for entry in payload.get("Items", [])],
        "total": payload.get("TotalRecordCount", 0),
        "start": start,
        "genres": genres,
    })


@app.get("/api/item/<item_id>")
@login_required
@_library_guard
def library_item(item_id: str):
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    connection = _library_connection()
    user_id = connection.get("user_id")
    fields = (
        "DateCreated,Genres,Overview,CommunityRating,OfficialRating,RunTimeTicks,"
        "People,Studios,RemoteTrailers,MediaSources,Taglines,OriginalTitle,MediaStreams"
    )
    item = _library_json(f"Users/{user_id}/Items/{item_id}", Fields=fields)
    if not item or not item.get("Id"):
        return _error("That title is no longer in your library.", 404)

    payload = _public_item(item, detail=True)
    payload["played"] = bool((item.get("UserData") or {}).get("Played"))

    if item.get("Type") == "BoxSet":
        children = _library_json(
            f"Users/{user_id}/Items", ParentId=item_id, Recursive="true",
            SortBy="SortName", SortOrder="Ascending", Fields=fields,
            ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Thumb",
        ).get("Items", [])
        payload["items"] = [_public_item(child) for child in children]

    if item.get("Type") == "Series":
        seasons = _library_json(
            f"Shows/{item_id}/Seasons", UserId=user_id, Fields=fields,
            ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Thumb",
        ).get("Items", [])
        episodes = _library_json(
            f"Shows/{item_id}/Episodes", UserId=user_id, Fields=fields + ",Overview",
            ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Thumb",
        ).get("Items", [])
        payload["seasons"] = [{
            "id": season.get("Id"),
            "name": season.get("Name"),
            "index": season.get("IndexNumber"),
            "episode_count": (season.get("UserData") or {}).get("UnplayedItemCount"),
            "poster": _art(season, "poster"),
        } for season in seasons]
        payload["episodes"] = [_public_item(episode) for episode in episodes]
        payload["episode_count"] = len(episodes)
        try:
            next_up = _library_json(
                "Shows/NextUp", UserId=user_id, SeriesId=item_id, Limit=1, Fields=fields,
                ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Thumb",
            ).get("Items", [])
            payload["next_up"] = _public_item(next_up[0]) if next_up else None
        except (requests.RequestException, KeyError, ValueError):
            payload["next_up"] = None

    return jsonify(payload)


@app.get("/api/item/<item_id>/similar")
@login_required
@_library_guard
def library_similar(item_id: str):
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    connection = _library_connection()
    user_id = connection.get("user_id")
    items = _library_json(
        f"Items/{item_id}/Similar", UserId=user_id, Limit=18,
        Fields="Genres,Overview,CommunityRating,OfficialRating,RunTimeTicks",
        ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Logo",
    ).get("Items", [])
    return jsonify({"items": [_public_item(entry) for entry in items]})


@app.get("/api/search/library")
@login_required
@_library_guard
def library_search():
    query = (request.args.get("q") or "").strip()
    connection = _library_connection()
    user_id = connection.get("user_id")
    if len(query) < 2:
        return jsonify({"movies": [], "shows": [], "episodes": [], "total": 0})

    payload = _library_json(
        f"Users/{user_id}/Items",
        SearchTerm=query[:120], Recursive="true",
        IncludeItemTypes="Movie,Series,Episode", Limit=36,
        Fields="Genres,Overview,CommunityRating,OfficialRating,RunTimeTicks,DateCreated",
        ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Logo,Thumb",
        SortBy="SortName", SortOrder="Ascending",
    )
    buckets: dict[str, list[dict]] = {"Movie": [], "Series": [], "Episode": []}
    for entry in payload.get("Items", []):
        bucket = buckets.get(entry.get("Type"))
        if bucket is not None:
            bucket.append(_public_item(entry))
    return jsonify({
        "movies": buckets["Movie"],
        "shows": buckets["Series"],
        "episodes": buckets["Episode"],
        "total": sum(len(bucket) for bucket in buckets.values()),
    })


@app.post("/api/item/<item_id>/played")
@login_required
@_library_guard
def library_set_played(item_id: str):
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    connection = _library_connection()
    user_id = connection.get("user_id")
    played = bool((request.get_json(silent=True) or {}).get("played", True))
    method = "POST" if played else "DELETE"
    response = _library_request(method, f"Users/{user_id}/PlayedItems/{item_id}")
    response.raise_for_status()
    return jsonify({"played": played})


@app.post("/api/item/<item_id>/favorite")
@login_required
@_library_guard
def library_set_favorite(item_id: str):
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    connection = _library_connection()
    user_id = connection.get("user_id")
    favorite = bool((request.get_json(silent=True) or {}).get("favorite", True))
    method = "POST" if favorite else "DELETE"
    response = _library_request(method, f"Users/{user_id}/FavoriteItems/{item_id}")
    response.raise_for_status()
    return jsonify({"favorite": favorite})


@app.get("/api/art/<item_id>/<kind>")
@login_required
def library_art(item_id: str, kind: str):
    """Stream artwork so Jellyfin credentials stay server-side."""
    if kind not in IMAGE_KINDS or not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown image.", 404)
    tag = request.args.get("tag", "")
    try:
        width = min(max(int(request.args.get("w", 0)), 0), 4000)
    except (TypeError, ValueError):
        width = 0

    params = {"quality": 86, "tag": tag} if tag else {"quality": 86}
    if width:
        params["maxWidth"] = width
        params["maxHeight"] = width * (3 if kind == "logo" else 2)
    try:
        upstream = _library_request(
            "GET", f"Items/{item_id}/Images/{IMAGE_KINDS[kind]}",
            params=params, stream=True, timeout=20,
        )
    except requests.RequestException as exc:
        return _error("Artwork is unavailable.", 502, detail=str(exc))
    if upstream.status_code != 200:
        upstream.close()
        return _error("Artwork is unavailable.", 404)

    response = app.response_class(
        upstream.iter_content(64 * 1024),
        mimetype=upstream.headers.get("Content-Type", "image/jpeg"),
    )
    # Artwork is content-addressed by `tag`, so it can be cached hard.
    response.headers["Cache-Control"] = (
        "public, max-age=31536000, immutable" if tag else "public, max-age=300"
    )
    response.headers["Vary"] = "Cookie"
    return response


@app.get("/api/art/person/<person_id>")
@login_required
def library_person_art(person_id: str):
    if not _ITEM_ID.fullmatch(person_id):
        return _error("Unknown image.", 404)
    try:
        upstream = _library_request(
            "GET", f"Items/{person_id}/Images/Primary",
            params={"quality": 84, "maxWidth": 160, "maxHeight": 160},
            stream=True, timeout=20,
        )
    except requests.RequestException:
        return _error("Artwork is unavailable.", 502)
    if upstream.status_code != 200:
        upstream.close()
        return _error("Artwork is unavailable.", 404)
    response = app.response_class(upstream.iter_content(32 * 1024), mimetype="image/jpeg")
    response.headers["Cache-Control"] = "public, max-age=86400"
    response.headers["Vary"] = "Cookie"
    return response


_API_KEY_PARAM = re.compile(r"([?&])(?:api_?key)=[^&\s\"']*", re.IGNORECASE)


def _strip_credentials(text: str) -> str:
    """Remove any token Jellyfin embedded in a playlist URL.

    Jellyfin bakes its access token into the URLs it generates for HLS
    playlists. Those URLs are handed to the browser, so the token has to go —
    this process adds authentication on the way back out.
    """
    cleaned = _API_KEY_PARAM.sub(r"\1", text)
    return cleaned.replace("?&", "?").replace("&&", "&").replace("?&", "?")


@app.route("/media/<path:rest>", methods=["GET", "HEAD"])
@login_required
def media_proxy(rest: str):
    """Pass video, HLS playlists and subtitles through to Jellyfin.

    Authentication is added here, so the browser never holds a Jellyfin
    credential, and Range headers are forwarded so seeking and direct play
    work exactly as they would against Jellyfin directly.
    """
    headers = {"Accept-Encoding": "identity"}
    for header in ("Range", "If-Range"):
        if request.headers.get(header):
            headers[header] = request.headers[header]
    # Never forward a browser-supplied token; this process supplies its own.
    params = {key: value for key, value in request.args.items() if key.lower() not in {"api_key", "apikey"}}

    try:
        upstream = _library_request(
            request.method, rest, params=params, headers=headers,
            stream=True, timeout=(15, 300),
        )
    except requests.RequestException as exc:
        return _error("Playback is unavailable right now.", 502, detail=str(exc))

    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    is_playlist = "mpegurl" in content_type or rest.endswith(".m3u8")

    if is_playlist and upstream.status_code == 200:
        body = _strip_credentials(upstream.text)
        upstream.close()
        response = app.response_class(body, status=200, mimetype="application/vnd.apple.mpegurl")
        response.headers["Cache-Control"] = "no-store"
        return response

    response = app.response_class(
        upstream.iter_content(256 * 1024),
        status=upstream.status_code,
        mimetype=content_type,
    )
    for header in ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
        if upstream.headers.get(header):
            response.headers[header] = upstream.headers[header]
    # Seeking needs the browser to know the resource is range-capable.
    response.headers.setdefault("Accept-Ranges", "bytes")
    response.headers["Cache-Control"] = "private, max-age=86400" if "/Trickplay/" in rest and upstream.status_code == 200 else "no-store"
    response.call_on_close(upstream.close)
    return response


# ── Playback negotiation ────────────────────────────────────────────────────

# What a Chromium/Firefox/Safari <video> can generally decode on its own. The
# client narrows this further with its own canPlayType probes.
DEFAULT_BROWSER_CAPS = {
    "containers": ["mp4", "m4v", "webm"],
    "video": ["h264", "vp8", "vp9", "av1"],
    "audio": ["aac", "mp3", "opus", "vorbis", "flac"],
    "max_channels": 2,
}

# Height ceiling for anything this server has to encode. 0 disables the cap.
DEFAULT_MAX_HEIGHT = 1080
MAX_MAX_HEIGHT = 2160


def _device_profile(caps: dict, max_bitrate: int, max_height: int = DEFAULT_MAX_HEIGHT) -> dict:
    containers = ",".join(caps.get("containers") or DEFAULT_BROWSER_CAPS["containers"])
    video_codecs = list(caps.get("video") or DEFAULT_BROWSER_CAPS["video"])
    audio = ",".join(caps.get("audio") or DEFAULT_BROWSER_CAPS["audio"])
    channels = str(caps.get("max_channels") or DEFAULT_BROWSER_CAPS["max_channels"])
    # Browsers report this only when they can genuinely decode HEVC, and it is
    # the difference between Jellyfin copying a 4K stream and re-encoding it.
    hevc_capable = "hevc" in video_codecs

    profile = {
        "MaxStreamingBitrate": max_bitrate,
        "MaxStaticBitrate": max_bitrate,
        "MusicStreamingTranscodingBitrate": 384000,
        "DirectPlayProfiles": [{
            "Container": containers, "Type": "Video",
            "VideoCodec": ",".join(video_codecs), "AudioCodec": audio,
        }],
        "TranscodingProfiles": [{
            "Container": "ts", "Type": "Video", "Protocol": "hls",
            "VideoCodec": "h264", "AudioCodec": "aac,mp3",
            "Context": "Streaming", "MaxAudioChannels": channels,
            "MinSegments": 1, "BreakOnNonKeyFrames": True,
            "EnableSubtitlesInManifest": True,
        }],
        "CodecProfiles": [],
        "SubtitleProfiles": [
            {"Format": format_, "Method": "External"}
            for format_ in ("vtt", "srt", "subrip", "ass", "ssa", "mov_text", "ttml")
        ],
    }

    if hevc_capable:
        # Copying HEVC into fragmented MP4 avoids decoding and re-encoding it.
        # Measured on a 4K HEVC source: ~18x realtime versus ~0.28x to encode
        # H.264, so the film starts almost immediately.
        #
        # This profile must list HEVC *alone*. Given "hevc,h264" Jellyfin
        # chooses to encode H.264 instead — verified by decoding the segments it
        # produced — so the H.264 fallback lives in its own profile below.
        profile["TranscodingProfiles"].insert(0, {
            "Container": "fmp4", "Type": "Video", "Protocol": "hls", "Context": "Streaming",
            "VideoCodec": "hevc", "AudioCodec": "aac,mp3",
            "MaxAudioChannels": channels, "MinSegments": 1,
            "BreakOnNonKeyFrames": False, "AllowVideoStreamCopy": True,
        })

    if max_height and max_height > 0:
        # Software-encoding 4K H.264 on a small server runs at roughly a quarter
        # of realtime, which is what makes playback take ~20s to start. Capping
        # the browser's output is ~2.5x faster and invisible on a laptop screen.
        profile["CodecProfiles"] = [{
            "Type": "Video", "Codec": "h264",
            "Conditions": [{
                "Condition": "LessThanEqual", "Property": "Width",
                "Value": str(max_height * 16 // 9), "IsRequired": True,
            }],
        }]
    return profile


PLAY_METHODS = {"DirectPlay": "DirectPlay", "DirectStream": "DirectStream", "Transcode": "Transcode"}


def _stream_urls(item_id: str, source: dict, play_session: str) -> dict:
    """Absolute in-app URLs for the negotiated stream."""
    source_id = source.get("Id")
    if source.get("SupportsDirectPlay"):
        return {
            "mode": "direct",
            "url": _media_url(f"Videos/{item_id}/stream", static="true",
                              mediaSourceId=source_id, playSessionId=play_session),
            "method": "DirectPlay",
        }
    transcoding = source.get("TranscodingUrl")
    if transcoding:
        # Jellyfin hands back a server-relative path; re-root it on /media and
        # drop the credential it embedded.
        path, _, query = transcoding.lstrip("/").partition("?")
        query = _strip_credentials("?" + query).lstrip("?")
        return {
            "mode": "hls",
            "url": f"/media/{path}" + (f"?{query}" if query else ""),
            "method": "Transcode" if not source.get("SupportsDirectStream") else "DirectStream",
        }
    return {
        "mode": "direct",
        "url": _media_url(f"Videos/{item_id}/stream", static="true", mediaSourceId=source_id),
        "method": "DirectPlay",
    }


def _video_is_reencoded(source: dict) -> bool:
    """Whether Jellyfin must re-encode the picture, as opposed to repackaging it.

    A stream that is only being recontainered (or whose audio alone is being
    converted) is not a quality compromise and should not be described as one.
    """
    query = (source.get("TranscodingUrl") or "").partition("?")[2]
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name == "TranscodeReasons":
            return "VideoCodecNotSupported" in unquote(value)
    return False


def _playback_tracks(item_id: str, source: dict) -> tuple[list[dict], list[dict]]:
    subtitles, audio = [], []
    for stream in source.get("MediaStreams") or []:
        index, kind = stream.get("Index"), stream.get("Type")
        if kind == "Subtitle":
            if stream.get("IsTextSubtitleStream") is False:
                continue
            subtitles.append({
                "index": index,
                "language": _language_name(stream.get("Language")),
                "title": _track_title(stream, index),
                "is_default": bool(stream.get("IsDefault")),
                "is_forced": bool(stream.get("IsForced")),
                "is_external": bool(stream.get("IsExternal")),
                "url": _media_url(
                    f"Videos/{item_id}/{source.get('Id')}/Subtitles/{index}/0/Stream.vtt"
                ),
            })
        elif kind == "Audio":
            audio.append({
                "index": index,
                "language": _language_name(stream.get("Language")),
                "title": _track_title(stream, index),
                "codec": (stream.get("Codec") or "").upper(),
                "channels": stream.get("ChannelLayout") or None,
                "is_default": bool(stream.get("IsDefault")),
            })
    return subtitles, audio


@app.post("/api/play/<item_id>")
@login_required
@_library_guard
def library_play(item_id: str):
    """Negotiate a playable stream for this browser."""
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    connection = _library_connection()
    user_id = connection.get("user_id")
    if not user_id:
        return _error("The watch library is not connected.", 409)

    payload = request.get_json(silent=True) or {}
    caps = payload.get("capabilities") or {}
    try:
        max_bitrate = max(500_000, min(int(payload.get("max_bitrate") or 20_000_000), 120_000_000))
    except (TypeError, ValueError):
        max_bitrate = 20_000_000
    try:
        max_height = int(payload.get("max_height", DEFAULT_MAX_HEIGHT))
    except (TypeError, ValueError):
        max_height = DEFAULT_MAX_HEIGHT
    max_height = 0 if max_height <= 0 else min(max_height, MAX_MAX_HEIGHT)

    body = {
        "UserId": user_id,
        "StartTimeTicks": max(0, int(payload.get("start_ticks") or 0)),
        "IsPlayback": True,
        "AutoOpenLiveStream": True,
        "EnableDirectPlay": True,
        "EnableDirectStream": True,
        "EnableTranscoding": True,
        "AllowVideoStreamCopy": True,
        "AllowAudioStreamCopy": True,
        "MaxStreamingBitrate": max_bitrate,
        "DeviceProfile": _device_profile(caps, max_bitrate, max_height),
    }
    if payload.get("media_source_id"):
        body["MediaSourceId"] = payload["media_source_id"]
    if payload.get("audio_index") is not None:
        body["AudioStreamIndex"] = int(payload["audio_index"])
    if payload.get("subtitle_index") is not None:
        body["SubtitleStreamIndex"] = int(payload["subtitle_index"])

    response = _library_request("POST", f"Items/{item_id}/PlaybackInfo", json=body)
    response.raise_for_status()
    result = response.json()

    sources = result.get("MediaSources") or []
    if not sources:
        return _error("This title can't be played right now.", 409)
    source = sources[0]
    play_session = result.get("PlaySessionId") or uuid.uuid4().hex

    subtitles, audio = _playback_tracks(item_id, source)
    stream = _stream_urls(item_id, source, play_session)

    item = _library_json(f"Users/{user_id}/Items/{item_id}", Fields="RunTimeTicks,MediaSources,Trickplay,Path")
    user_data = item.get("UserData") or {}
    runtime_ticks = item.get("RunTimeTicks") or source.get("RunTimeTicks") or 0
    start_ticks = body["StartTimeTicks"] or (user_data.get("PlaybackPositionTicks") or 0)

    next_up = None
    if item.get("Type") == "Episode" and item.get("SeriesId"):
        try:
            candidates = _library_json(
                "Shows/NextUp", UserId=user_id, SeriesId=item["SeriesId"],
                Limit=4, Fields="RunTimeTicks,Overview",
                ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Thumb",
            ).get("Items", [])
            following = next(
                (entry for entry in candidates if entry.get("Id") != item_id), None
            )
            if not following:
                following = _next_episode_in_sequence(item, user_id)
            next_up = _public_item(following) if following else None
        except (requests.RequestException, KeyError, ValueError):
            next_up = None

    return jsonify({
        "item_id": item_id,
        "media_source_id": source.get("Id"),
        "previews": _preview_descriptor(item, source.get("Id")),
        "play_session_id": play_session,
        "mode": stream["mode"],
        "method": stream["method"],
        "url": stream["url"],
        "container": (source.get("Container") or "").split(",")[0].upper(),
        "runtime_ticks": runtime_ticks,
        "start_ticks": int(start_ticks or 0),
        "title": item.get("Name"),
        "subtitles": subtitles,
        "audio": audio,
        "next_up": next_up,
        "transcodes": stream["method"] == "Transcode",
        "video_reencoded": _video_is_reencoded(source),
        "player": {"title": item.get("Name"), "item": _public_item(item)},
    })


def _imported_preview_entry(item, source_id):
    source = next((s for s in item.get("MediaSources", []) if s.get("Id") == source_id), None)
    if not source and source_id != item.get("Id"):
        return None
    source_path = source.get("Path") if source else item.get("Path")
    if _ITEM_ID.fullmatch(source_id or ""):
        generated = ROOT / "data" / "preview-cache" / source_id / "manifest.json"
        try:
            entry = json.loads(generated.read_text())
            if entry.get("media_path") == source_path and entry.get("previews"):
                return {**entry, "cache_type": "generated"}
        except (OSError, ValueError):
            pass
    manifest = ROOT / "data" / "tv-migration" / "manifest.json"
    try:
        entries = json.loads(manifest.read_text()).get("entries", [])
    except (OSError, ValueError):
        return None
    return next((e for e in entries if e.get("previews")
                 and source_path == str(SHOWS_PATH / e.get("relative", ""))), None)


@app.get("/api/previews/<item_id>/<source_id>/<int:index>.jpg")
@login_required
@_library_guard
def imported_preview_image(item_id, source_id, index):
    if not _ITEM_ID.fullmatch(item_id) or not _ITEM_ID.fullmatch(source_id):
        return _error("Unknown preview.", 404)
    user_id = _library_connection().get("user_id")
    item = _library_json(f"Users/{user_id}/Items/{item_id}", Fields="Path,MediaSources")
    entry = _imported_preview_entry(item, source_id)
    if not entry:
        return _error("Preview is unavailable.", 404)
    info = entry["previews"]
    sheets = (info["count"] + info["columns"] * info["rows"] - 1) // (info["columns"] * info["rows"])
    asset = info["asset_id"]
    if index >= sheets or not _ITEM_ID.fullmatch(asset):
        return _error("Unknown preview.", 404)
    base = ROOT / "data" / "preview-cache" if entry.get("cache_type") == "generated" else ROOT / "data" / "tv-migration" / "previews"
    path = base / asset / f"{index}.jpg"
    if not path.is_file():
        return _error("Preview is unavailable.", 404)
    response = send_file(path, mimetype="image/jpeg", max_age=86400)
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


def _preview_descriptor(item, source_id):
    # A different version may have a different timeline: never use another source's frames.
    sizes = (item.get("Trickplay") or {}).get(source_id, {})
    if not sizes:
        entry = _imported_preview_entry(item, source_id)
        if not entry:
            return None
        values = {k: v for k, v in entry["previews"].items() if k != "asset_id"}
        values["url"] = f"/api/previews/{item['Id']}/{source_id}/{{index}}.jpg"
        return values
    options = sorted((int(width), info) for width, info in sizes.items() if str(width).isdigit())
    if not options:
        return None
    width, info = next(((w, i) for w, i in options if w >= 200), options[-1])
    values = {"width": width, "height": info.get("Height"), "columns": info.get("TileWidth"),
              "rows": info.get("TileHeight"), "count": info.get("ThumbnailCount"), "interval": info.get("Interval")}
    if not all(isinstance(v, int) and v > 0 for v in values.values()):
        return None
    values["url"] = f"/media/Videos/{item['Id']}/Trickplay/{width}/{{index}}.jpg?MediaSourceId={source_id}"
    return values


def _next_episode_in_sequence(item: dict, user_id: str) -> dict | None:
    """Fall back to the next episode by number when NextUp has no opinion."""
    if not item.get("SeriesId") or item.get("IndexNumber") is None:
        return None
    episodes = _library_json(
        f"Shows/{item['SeriesId']}/Episodes", UserId=user_id,
        SeasonId=item.get("SeasonId"), Fields="RunTimeTicks,Overview",
        ImageTypeLimit=1, EnableImageTypes="Primary,Backdrop,Thumb",
    ).get("Items", [])
    ordered = sorted(
        (entry for entry in episodes if entry.get("IndexNumber") is not None),
        key=lambda entry: entry["IndexNumber"],
    )
    for index, entry in enumerate(ordered):
        if entry.get("Id") == item.get("Id") and index + 1 < len(ordered):
            return ordered[index + 1]
    return None


def _report_playback(item_id: str, endpoint: str, payload: dict) -> None:
    connection = _library_connection()
    body = {
        "ItemId": item_id,
        "MediaSourceId": payload.get("media_source_id"),
        "PlaySessionId": payload.get("play_session_id"),
        "PositionTicks": max(0, int(payload.get("position_ticks") or 0)),
        "IsPaused": bool(payload.get("is_paused")),
        "IsMuted": bool(payload.get("is_muted")),
        "PlayMethod": PLAY_METHODS.get(payload.get("method"), "Transcode"),
        "RepeatMode": "RepeatNone",
        "VolumeLevel": int(payload.get("volume") or 100),
        "CanSeek": True,
        "PlaybackStartTimeTicks": max(0, int(payload.get("position_ticks") or 0)),
    }
    if payload.get("audio_index") is not None:
        body["AudioStreamIndex"] = int(payload["audio_index"])
    if payload.get("subtitle_index") is not None:
        body["SubtitleStreamIndex"] = int(payload["subtitle_index"])
    body = {key: value for key, value in body.items() if value is not None}

    response = _library_request("POST", f"Sessions/{endpoint}", json=body)
    response.raise_for_status()


@app.post("/api/play/<item_id>/started")
@login_required
def library_play_started(item_id: str):
    """Announce the start of playback.

    Jellyfin attaches resume positions to a playback session, so without this
    the progress reports have nothing to update and Continue Watching never
    learns where the viewer got to.
    """
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    payload = request.get_json(silent=True) or {}
    try:
        _report_playback(item_id, "Playing", payload)
    except requests.RequestException as exc:
        return _error("Couldn't start playback tracking.", 502, detail=str(exc))
    return ("", 204)


@app.post("/api/play/<item_id>/progress")
@login_required
def library_play_progress(item_id: str):
    """Keep Jellyfin's resume position authoritative — this is what makes
    Continue Watching correct everywhere, not just in this app."""
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    payload = request.get_json(silent=True) or {}
    try:
        _report_playback(item_id, "Playing/Progress", payload)
    except requests.RequestException as exc:
        return _error("Couldn't save your position.", 502, detail=str(exc))
    return ("", 204)


@app.post("/api/play/<item_id>/stopped")
@login_required
def library_play_stopped(item_id: str):
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    payload = request.get_json(silent=True) or {}
    try:
        _report_playback(item_id, "Playing/Stopped", payload)
    except requests.RequestException as exc:
        return _error("Couldn't save your position.", 502, detail=str(exc))
    return ("", 204)


_EPISODE_IN_NAME = re.compile(r"\bS\d{1,2}\s?E\d{1,3}\b", re.IGNORECASE)


def _merge_candidates(*groups: list[dict]) -> list[dict]:
    """Candidates from several searches, without duplicates."""
    merged: list[dict] = []
    seen: set[tuple] = set()
    for group in groups:
        for candidate in group:
            key = (
                (candidate.get("Name") or "").strip().lower(),
                candidate.get("ProductionYear"),
                (candidate.get("ProviderIds") or {}).get("Tmdb"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(candidate)
    return merged


def _metadata_query(name: str) -> str:
    """The string worth searching an indexer for.

    Raw library names carry release-site prefixes and quality tags that stop a
    metadata lookup dead, so the display cleanup runs first.
    """
    cleaned = _display_title(name)
    return re.sub(r"\s*[(\[]\d{4}[)\]]\s*$", "", cleaned).strip()


# Languages that release folders name outright, and that an indexer's search
# needs to tell one film from another of the same name.
_RELEASE_LANGUAGES = (
    "malayalam", "tamil", "telugu", "hindi", "kannada", "bengali", "marathi",
    "punjabi", "english", "urdu", "gujarati", "malay",
)
_YEAR_IN_NAME = re.compile(r"\b(19|20)\d{2}\b")


def _release_hints(raw_name: str) -> tuple[str | None, list[str]]:
    """The year and the languages a release name states about itself."""
    lowered = (raw_name or "").lower()
    year = None
    years = _YEAR_IN_NAME.findall(lowered)
    if years:
        year = str(re.findall(r"\b(?:19|20)\d{2}\b", lowered)[0])
    languages = [name for name in _RELEASE_LANGUAGES
                 if re.search(rf"\b{name}\b", lowered)]
    return year, languages


_METADATA_MARKER = re.compile(
    r"[\(\[]|\b(?:19|20)\d{2}\b|\b(?:2160p|1440p|1080p|720p|480p|bluray|blu-ray|brrip|bdrip|"
    r"webrip|web-dl|webdl|hdrip|dvdrip|hdtv|hdcam|telesync|x264|x265|h\.?264|h\.?265|hevc|avc|"
    r"aac|ddp|ac3|dts|imax|10bit|5\.1|7\.1|hq|hd|esub|esubs)\b",
    re.IGNORECASE,
)


def _release_title(raw_name: str) -> str:
    """The film's own name, as the file on disk states it.

    Everything from the first sign of release metadata onwards is dropped —
    "(2026)", "1080p", "HQ HDRip" — because the search wants a title, not a
    filename. The year and the language are kept aside as hints instead.
    """
    if not raw_name:
        return ""
    text = _LEADING_DOMAIN.sub("", _SITE_PREFIX.sub("", Path(raw_name).stem if "/" in raw_name or "." in Path(raw_name).name else raw_name))
    marker = _METADATA_MARKER.search(text)
    if marker:
        text = text[: marker.start()]
    return re.sub(r"\s+", " ", text).strip(" .-_")


def _metadata_queries(name: str, raw_name: str) -> list[str]:
    """The search strings worth trying for a title that was never matched.

    A release folder carries two things a search wants and the display cleanup
    throws away: the year, and the language — "… Spa (2026) Malayalam HQ HDRip".
    Searching "Spa" alone answers with Hollywood films of that name; searching
    the whole thing answers with the Malayalam one.
    """
    cleaned = _metadata_query(name)
    year, languages = _release_hints(raw_name)
    queries: list[str] = []

    # The release's own name, when the library's name differs from it: an item
    # matched to the wrong film keeps the wrong name, and only the file on disk
    # still says what it really is.
    def with_hints(core: str, language: str | None = None) -> str:
        parts = [core]
        if year and year not in core:
            parts.append(year)
        if language and language not in core.lower():
            parts.append(language)
        return " ".join(parts)

    from_file = _release_title(raw_name)
    if from_file and from_file.lower() != cleaned.lower():
        for language in languages[:1]:
            queries.append(with_hints(from_file, language))
        queries.append(with_hints(from_file))

    for language in languages[:1]:
        queries.append(with_hints(cleaned, language))
    queries.append(with_hints(cleaned))
    queries.append(cleaned)
    return list(dict.fromkeys(query for query in queries if len(query) >= 2))[:3]


def _needs_metadata(item: dict) -> bool:
    return not (item.get("Overview") or "").strip() or not item.get("Genres")


@app.post("/api/library/refresh")
@admin_required
@_library_guard
def library_refresh():
    """Ask Jellyfin to rescan the library."""
    _refresh_library_mount()
    ok = _refresh_library()
    if not ok:
        return _error("Your media server did not accept the rescan request.", 502)
    return jsonify({"ok": True})


def _metadata_candidate(candidate: dict) -> dict:
    """One search result, shaped for the interface to offer."""
    return {
        "name": candidate.get("Name"),
        "year": candidate.get("ProductionYear"),
        "overview": (candidate.get("Overview") or "")[:280] or None,
        "image": candidate.get("ImageUrl"),
        "provider_ids": candidate.get("ProviderIds") or {},
        "result": candidate,
    }


@app.post("/api/library/metadata/find")
@admin_required
@_library_guard
def metadata_find():
    """Search for the film a title really is, with a query the viewer can edit.

    A release name says more than the library does: "… Spa (2026) Malayalam
    HQ HDRip …" carries the year and the language, which is the difference
    between a Hollywood film of the same name and the one that was downloaded.
    The suggested query is built from the file itself, and can be rewritten.
    """
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("item_id", ""))
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    typed = str(payload.get("query") or "").strip()[:120]

    item = _server_library_item(item_id, "Name,Path,ProductionYear")
    if not item:
        return _error("That title is no longer in your library.", 404)
    name, path = item.get("Name") or "", item.get("Path") or ""
    suggested = _metadata_queries(name, path)[0]
    queries = [typed] if typed else _metadata_queries(name, path)[:3]

    found: list[dict] = []
    for query in queries:
        if len(query) < 2:
            continue
        try:
            response = _library_request("POST", "Items/RemoteSearch/Movie", json={
                "SearchInfo": {
                    "Name": query,
                    # A typed query is the viewer's own; the film's year is only
                    # a hint when the query is the one we suggested.
                    "Year": None if typed else item.get("ProductionYear"),
                    "IncludeAdult": False,
                },
            })
            response.raise_for_status()
            found = _merge_candidates(found, response.json() or [])
        except (requests.RequestException, ValueError):
            continue
        if len(found) >= 6:
            break

    return jsonify({
        "suggested_query": suggested,
        "queries": queries,
        "candidates": [_metadata_candidate(candidate) for candidate in found[:8]],
    })


@app.post("/api/library/metadata/scan")
@admin_required
@_library_guard
def metadata_scan():
    """Find library entries that were never matched to a real film.

    Downloads named by their release folder often land as an unmatched title
    with no synopsis, genres or backdrop. This asks Jellyfin's own metadata
    providers what each one probably is, without changing anything.
    """
    payload = _library_json(
        "Items", Recursive="true", IncludeItemTypes="Movie", Limit=1000,
        Fields="Overview,Genres,ProductionYear,ProviderIds,Path",
    )
    results, skipped = [], []
    for item in payload.get("Items", []):
        if not _needs_metadata(item):
            continue
        raw = item.get("Name", "")
        if _EPISODE_IN_NAME.search(raw):
            # Almost always a TV episode filed as a movie; matching it to a
            # film would be actively wrong.
            skipped.append({"id": item["Id"], "name": _display_title(raw), "reason": "episode"})
            continue
        queries = _metadata_queries(raw, item.get("Path") or raw)
        if not queries or len(queries[0]) < 2:
            skipped.append({"id": item["Id"], "name": raw, "reason": "unsearchable"})
            continue

        year = item.get("ProductionYear")
        candidates: list[dict] = []
        query = queries[0]
        for attempt, query in enumerate(queries):
            try:
                response = _library_request("POST", "Items/RemoteSearch/Movie", json={
                    "SearchInfo": {
                        "Name": query,
                        "Year": year,
                        "IncludeAdult": False,
                    },
                })
                response.raise_for_status()
                found = response.json() or []
            except (requests.RequestException, ValueError):
                found = []
            if found:
                candidates = _merge_candidates(candidates, found)
                # Good enough to choose from: the year agrees, so stop asking.
                if any(candidate.get("ProductionYear") == year for candidate in found):
                    break
            if attempt >= 1 and candidates:
                break

        # Prefer a candidate whose year agrees with the folder we downloaded.
        candidates.sort(key=lambda candidate: (
            0 if year and candidate.get("ProductionYear") == year else 1,
            -(candidate.get("ProductionYear") or 0),
        ))
        candidates = candidates[:6]
        results.append({
            "id": item["Id"],
            "name": _display_title(raw),
            "raw_name": raw,
            "year": year,
            "missing": [
                label for label, present in (
                    ("description", bool((item.get("Overview") or "").strip())),
                    ("genres", bool(item.get("Genres"))),
                ) if not present
            ],
            "query": query,
            "queries_tried": queries[:2],
            "candidates": [{
                "name": candidate.get("Name"),
                "year": candidate.get("ProductionYear"),
                "overview": (candidate.get("Overview") or "")[:280] or None,
                "image": candidate.get("ImageUrl"),
                "provider_ids": candidate.get("ProviderIds") or {},
                "result": candidate,
            } for candidate in candidates[:4]],
        })
    return jsonify({"items": results, "skipped": skipped})


@app.post("/api/library/metadata/apply")
@admin_required
@_library_guard
def metadata_apply():
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("item_id", ""))
    candidate = payload.get("candidate")
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    if not isinstance(candidate, dict) or not candidate:
        return _error("Choose a match to apply.")

    response = _library_request(
        "POST", f"Items/RemoteSearch/Apply/{item_id}",
        params={"replaceAllImages": "true"},
        json=candidate,
    )
    response.raise_for_status()
    return jsonify({"applied": True})


@app.get("/api/library/stats")
@login_required
@_library_guard
def library_stats():
    """Sizes and counts for the storage section of Settings."""
    connection = _library_connection()
    user_id = connection.get("user_id")
    totals = {"Movie": 0, "Series": 0, "Episode": 0}
    size = 0
    for item_type in ("Movie", "Series", "Episode"):
        payload = _library_json(
            f"Users/{user_id}/Items", Recursive="true", IncludeItemTypes=item_type,
            Limit=1, Fields="", EnableImages="false",
        )
        totals[item_type] = payload.get("TotalRecordCount", 0)
    for item_type in ("Movie", "Episode"):
        start = 0
        while True:
            payload = _library_json(
                f"Users/{user_id}/Items", Recursive="true", IncludeItemTypes=item_type,
                StartIndex=start, Limit=200, Fields="MediaSources", EnableImages="false",
            )
            entries = payload.get("Items", [])
            for entry in entries:
                for source in entry.get("MediaSources") or []:
                    size += source.get("Size") or 0
            start += len(entries)
            if not entries or start >= payload.get("TotalRecordCount", 0):
                break
    try:
        folders = _library_request("GET", "Library/VirtualFolders").json()
    except (requests.RequestException, ValueError):
        folders = []
    return jsonify({
        "counts": totals,
        "total_size": size,
        "folders": [{
            "name": folder.get("Name"),
            "locations": folder.get("Locations") or [],
            "type": folder.get("CollectionType"),
        } for folder in folders],
        "library_path": str(LIBRARY_PATH),
        "library_remote": LIBRARY_REMOTE,
    })


# ═══════════════════════════════════════════════════════════════════════════
# AI clips.
#
# Generating clips is a long job — subtitles, a model, ffmpeg and an upload —
# so it runs on a thread behind the same job registry downloads use, and the
# detail page polls it. The pipeline itself lives in the `clips` package and
# knows nothing about Flask: everything it needs from this application arrives
# through the pieces below.
# ═══════════════════════════════════════════════════════════════════════════

CLIP_LOCK = threading.Lock()


def _safe_folder_name(name: str) -> str:
    """A folder name Drive and Jellyfin both accept, derived from a filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(". ")
    return cleaned[:120] or "movie"


def _server_library_item(item_id: str, fields: str) -> dict | None:
    """One item, read with the server key.

    Members cannot see filesystem paths through their own token — Jellyfin
    withholds `Path` from non-administrators — and clips have to be written
    next to the file they came from, so this uses the server credential and
    keeps the path server-side.
    """
    token = _server_jellyfin_token()
    if not token:
        return None
    response = requests.get(
        f"{JELLYFIN_URL}/Items",
        headers=_jellyfin_headers(token, ""),
        params={"Ids": item_id, "Fields": fields, "Limit": 1},
        timeout=15,
    )
    response.raise_for_status()
    items = response.json().get("Items") or []
    return items[0] if items else None


def _clip_media(item_id: str) -> tuple[clip_pipeline.ClipMedia | None, str | None]:
    """Resolve everything the pipeline needs about one movie, or say why not."""
    if not _server_jellyfin_token():
        return None, "Popcorn has no Jellyfin API key, so it cannot find this movie's file."
    try:
        item = _server_library_item(item_id, "Path,RunTimeTicks,Genres,ProductionYear,Name")
    except requests.RequestException:
        return None, "Your media server could not be reached."
    except (KeyError, ValueError):
        return None, "Your media server returned something unexpected."
    if not item:
        return None, "That title is no longer in your library."
    if item.get("Type") != "Movie":
        return None, "Clips can only be generated for films."

    media_path = Path(item.get("Path") or "")
    if not str(media_path) or not media_path.name:
        return None, "Your media server did not report a file for this title."
    try:
        relative = media_path.relative_to(LIBRARY_PATH)
    except ValueError:
        return None, (
            f"This title is not stored in the Popcorn library ({LIBRARY_REMOTE}), "
            "so its clips cannot be saved beside it."
        )
    if not relative.parts or ".." in relative.parts:
        return None, "This movie's file is not in a place clips can be saved to."

    # A movie inside a release folder keeps its clips in that folder; a movie
    # that is a single file at the library root gets a folder of its own, named
    # after the file, so one movie's clips never land in another's.
    folder = relative.parent if str(relative.parent) != "." else Path(_safe_folder_name(relative.stem))
    clips_subdir = f"{folder.as_posix()}/generated-clips"

    runtime_ticks = item.get("RunTimeTicks") or 0
    return clip_pipeline.ClipMedia(
        item_id=item_id,
        title=item.get("Name") or "",
        year=item.get("ProductionYear"),
        genres=list(item.get("Genres") or []),
        runtime_seconds=round(runtime_ticks / TICKS_PER_SECOND) if runtime_ticks else None,
        source_path=str(media_path),
        clips_subdir=clips_subdir,
        remote_dir=f"{LIBRARY_REMOTE}/{clips_subdir}",
    ), None


class _ClipSubtitleSource:
    """Subtitles through the media server: what it already holds, and what its
    providers can fetch.

    This is the second-cheapest source of dialogue after a sidecar file: it
    covers subtitles embedded in the container as well as external ones, and
    Jellyfin does the extracting. Its providers (Open Subtitles, once it is
    configured) can also find subtitles for a film that has none at all.
    """

    def tracks(self, item_id: str) -> list[dict]:
        item = _server_library_item(item_id, "MediaSources")
        if not item:
            return []
        tracks = []
        for source in item.get("MediaSources") or []:
            for stream in source.get("MediaStreams") or []:
                if stream.get("Type") != "Subtitle":
                    continue
                if stream.get("IsTextSubtitleStream") is False:
                    continue
                tracks.append({
                    "index": stream.get("Index"),
                    "source_id": source.get("Id"),
                    "language": (stream.get("Language") or "").lower()[:3] or None,
                    "title": stream.get("DisplayTitle") or stream.get("Title"),
                    "is_default": bool(stream.get("IsDefault")),
                    "is_forced": bool(stream.get("IsForced")),
                })
        return tracks

    def fetch(self, item_id: str, track: dict) -> str:
        token = _server_jellyfin_token()
        if not token:
            return ""
        response = requests.get(
            f"{JELLYFIN_URL}/Videos/{item_id}/{track['source_id']}/Subtitles/"
            f"{track['index']}/0/Stream.srt",
            headers=_jellyfin_headers(token, ""),
            timeout=120,
        )
        if response.status_code != 200:
            return ""
        return response.text

    def search(self, item_id: str, language: str) -> list[dict]:
        """Ask every subtitle provider the media server has for this film."""
        token = _server_jellyfin_token()
        if not token:
            return []
        found: list[dict] = []
        for code in dict.fromkeys([language, language[:2]]):
            response = requests.get(
                f"{JELLYFIN_URL}/Items/{item_id}/RemoteSearch/Subtitles/{code}",
                headers=_jellyfin_headers(token, ""),
                timeout=60,
            )
            if response.status_code != 200:
                continue
            try:
                entries = response.json() or []
            except ValueError:
                continue
            for entry in entries:
                found.append({
                    "id": str(entry.get("Id")),
                    "name": entry.get("Name"),
                    "language": (entry.get("Language") or "").lower()[:3] or None,
                    "format": entry.get("Format"),
                    "downloads": entry.get("DownloadCount"),
                    "hash_match": bool(entry.get("IsHashMatch")),
                    "provider": entry.get("ProviderName"),
                    "hearing_impaired": bool(entry.get("IsHearingImpaired")),
                })
            if found:
                break
        return found

    def apply(self, item_id: str, subtitle_id: str) -> bool:
        """Have the media server download it and attach it to the film."""
        token = _server_jellyfin_token()
        if not token or not subtitle_id:
            return False
        response = requests.post(
            f"{JELLYFIN_URL}/Items/{item_id}/RemoteSearch/Subtitles/{quote(subtitle_id)}",
            headers=_jellyfin_headers(token, ""),
            timeout=180,
        )
        return response.status_code < 400

    def remove(self, item_id: str, track: dict) -> None:
        """Detach a subtitle that turned out to be for a different release.

        Leaving it attached would offer it during playback, which is worse than
        having no subtitles at all.
        """
        token = _server_jellyfin_token()
        if not token or not track:
            return
        try:
            requests.delete(
                f"{JELLYFIN_URL}/Videos/{item_id}/{track.get('source_id')}/Subtitles/{track.get('index')}",
                headers=_jellyfin_headers(token, ""),
                timeout=60,
            )
        except requests.RequestException:
            pass


class _ClipUploader:
    """Puts finished files on Drive, through the same rclone remote downloads
    already use — no second storage system, no second client."""

    def upload(self, work_dir: Path, remote_dir: str, hooks) -> None:
        command = [
            "rclone", "sync", str(work_dir), remote_dir,
            "--transfers=4", "--drive-chunk-size=64M",
            "--exclude", f"{clip_pipeline.TEMP_SUBDIR}/**",
        ]
        process = hooks.spawn(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output, _ = process.communicate()
        if process.returncode != 0:
            last = (output or "").strip().splitlines()[-1:] or ["rclone failed"]
            raise RuntimeError(f"The clips could not be saved to {remote_dir}: {last[0]}")
        # The mount caches directory listings for a day, so the new folder has
        # to be announced before it can be streamed back.
        _refresh_library_mount()

    def store_subtitle(self, media, name: str, segments: list) -> bool:
        """Write a verified subtitle beside the film on Drive.

        Once it is there it is the film's own subtitle: the media server picks
        it up for playback, and it outlives anything Popcorn knows. A subtitle
        already sitting there is left alone — it is not this job's to replace.
        """
        movie_dir = media.clips_subdir.rsplit("/generated-clips", 1)[0]
        remote_dir = f"{LIBRARY_REMOTE}/{movie_dir}" if movie_dir else LIBRARY_REMOTE
        directory = Path(movie_dir) if movie_dir else Path()

        existing = subprocess.run(
            ["rclone", "lsf", remote_dir, "--files-only", "--include", name],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return False

        with tempfile.TemporaryDirectory(prefix="popcorn-sub-", dir=ROOT) as temporary:
            target = Path(temporary) / name
            if not clip_render.write_srt(segments, target):
                return False
            copy = subprocess.run(
                ["rclone", "copy", temporary, remote_dir, "--transfers=1"],
                capture_output=True, text=True, timeout=300, check=False,
            )
            if copy.returncode != 0:
                raise RuntimeError(
                    (copy.stderr or "").strip().splitlines()[-1:] or ["rclone failed"]
                )
            # The mount lists directories for a day at a time; Jellyfin needs a
            # rescan to notice a new sidecar file.
            _refresh_library_mount()
            _refresh_library()
            app.logger.info("Saved subtitle %s beside %s", name, directory or "the library root")
        return True


class _RegisteredProcess:
    """A subprocess the Stop button can kill, unregistered when it exits."""

    def __init__(self, process: subprocess.Popen, job_id: str):
        self._process = process
        self._job_id = job_id

    def __getattr__(self, name):
        return getattr(self._process, name)

    def wait(self, *args, **kwargs):
        try:
            return self._process.wait(*args, **kwargs)
        finally:
            _unregister_job_process(self._job_id, self._process)


def _clip_hooks(job_id: str, work_dir: Path) -> clip_pipeline.Hooks:
    """Bind a running job to the pipeline's callbacks.

    Stage changes are few and are always written out; progress is not. Clip
    jobs report progress far more often than a download does — every subtitle
    segment during transcription — and persisting each one would mean
    thousands of database writes for a single film.
    """
    state = {"persisted_at": 0.0, "done": -1}

    def on_stage(status: str, message: str) -> None:
        state["persisted_at"] = time.time()
        state["done"] = -1
        _update_job(
            job_id, persist=True, status=status,
            stage_label=clip_pipeline.STAGE_LABELS.get(status, status),
            message=message, done=0, total=0, progress=0,
        )

    def on_progress(done: int, total: int, label: str = "") -> None:
        now = time.time()
        # Clip-by-clip progress is what the interface counts ("Generating 4 of
        # 12"), so every change there is kept; everything else is throttled.
        clip_step = (JOBS.get(job_id, {}).get("status") == clip_pipeline.GENERATING_CLIPS
                     and done != state["done"])
        persist = clip_step or (now - state["persisted_at"]) >= 1.0
        if persist:
            state["persisted_at"] = now
            state["done"] = done
        _update_job(
            job_id, persist=persist, done=done, total=total,
            progress=int(100 * done / total) if total else 0,
            message=label or JOBS.get(job_id, {}).get("message", ""),
        )

    def spawn(command, **kwargs):
        process = subprocess.Popen(command, cwd=ROOT, start_new_session=True, **kwargs)
        _register_job_process(job_id, process)
        return _RegisteredProcess(process, job_id)

    return clip_pipeline.Hooks(
        on_stage=on_stage,
        on_progress=on_progress,
        is_cancelled=lambda: _job_cancel_requested(job_id),
        spawn=spawn,
        refresh_mount=_refresh_library_mount,
        work_dir=work_dir,
        log=lambda message: _job_notes(job_id, message),
    )


def _clip_jobs(item_id: str | None = None, *, active_only: bool = False) -> list[dict]:
    with STATE_LOCK:
        jobs = [
            job for job in JOBS.values()
            if job.get("kind") == "clips" and (item_id is None or job.get("item_id") == item_id)
        ]
    if active_only:
        jobs = [job for job in jobs if job.get("status") in CLIP_ACTIVE_STATES | {"cancelling"}]
    jobs.sort(key=lambda job: job["created_at"], reverse=True)
    return jobs


def _clip_public(record: dict) -> dict:
    """One clip, shaped for the detail page."""
    segments = record.get("subtitle_segments") or []
    return {
        "id": record["clip_id"],
        "position": record.get("position"),
        "title": record.get("title"),
        "summary": record.get("summary"),
        "hook": record.get("hook"),
        "category": record.get("category"),
        "reason": record.get("reason"),
        "start": record.get("start_seconds"),
        "end": record.get("end_seconds"),
        "duration": record.get("duration"),
        "scores": {
            "interest": record.get("interest_score"),
            "context": record.get("context_score"),
            "dialogue": record.get("dialogue_score"),
            "visual": record.get("visual_score"),
            "final": record.get("final_score"),
        },
        "subtitle_count": len(segments),
        "size_bytes": record.get("size_bytes"),
        "profile": record.get("profile"),
        "created_at": record.get("created_at"),
        "stream_url": url_for("clip_stream", clip_id=record["clip_id"]),
        "download_url": url_for("clip_stream", clip_id=record["clip_id"], download="1"),
        "thumb_url": url_for("clip_thumb", clip_id=record["clip_id"])
        if record.get("thumb_name") else None,
    }


@app.get("/api/clips/<item_id>")
@login_required
def clips_state(item_id: str):
    """Everything the detail page needs to draw the clips section."""
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    clips = [_clip_public(record) for record in clip_store.load_clips(item_id)]
    analysis = clip_store.load_analysis(item_id)
    jobs = _clip_jobs(item_id)
    active = next((job for job in jobs if job.get("status") in CLIP_ACTIVE_STATES
                   or job.get("status") == "cancelling"), None)

    # The most recent job speaks for the movie: if a regeneration failed or was
    # stopped, saying "Completed" because older clips are still there would be
    # a lie about what just happened.
    state, message = None, ""
    if active:
        state, message = active.get("status"), active.get("message", "")
    elif jobs:
        state, message = jobs[0].get("status"), jobs[0].get("message", "")
        if state == clip_pipeline.COMPLETED and not clips:
            state = None
            message = "The clips for this film are no longer in your library."
    elif clips:
        state = clip_pipeline.COMPLETED
        message = f"{len(clips)} clips ready to watch."

    media, reason = (None, None)
    if not active:
        # Only worth asking when the viewer could actually start something.
        media, reason = _clip_media(item_id)
        if media is None and not clips and not jobs:
            # Nothing stored and nothing resolvable: this is not a movie we have.
            return _error(reason or "Unknown title.", 404)
    config = clip_analysis.LLMConfig.from_env()
    settings = _load_settings()

    payload = {
        "item_id": item_id,
        "state": state,
        "message": message,
        "done": (active or {}).get("done", 0),
        "total": (active or {}).get("total", 0),
        "progress": (active or {}).get("progress", 0),
        "stage_label": (active or {}).get("stage_label"),
        "job_id": (active or {}).get("id"),
        "can_stop": bool(active and active.get("status") != "cancelling"),
        "clips": clips,
        "can_generate": bool(media) and config.configured,
        "reason": reason if not config.configured
        else (None if media else reason),
        "llm_configured": config.configured,
        "vision_enabled": bool(settings["clip_vision"] and config.vision_configured),
        "settings": {
            "target_count": settings["clip_target_count"],
            "min_seconds": settings["clip_min_seconds"],
            "max_seconds": settings["clip_max_seconds"],
        },
        "analysis": {
            "cached": bool(analysis),
            "candidates": len(analysis.get("candidates") or []) if analysis else 0,
            "transcript_source": analysis.get("transcript_source") if analysis else None,
            "transcript_detail": analysis.get("transcript_detail") if analysis else None,
            "version": analysis.get("analysis_version") if analysis else None,
            "updated_at": analysis.get("updated_at") if analysis else None,
        } if analysis else None,
        "notes": (jobs[0].get("notes") or [])[-6:] if jobs else [],
        # The raw cause, kept for the diagnostics disclosure rather than shown
        # as the headline reason a job stopped.
        "detail": (active or (jobs[0] if jobs else {})).get("detail"),
        "updated_at": (active or (jobs[0] if jobs else {})).get("updated_at"),
    }
    return jsonify(payload)


@app.post("/api/clips/<item_id>/generate")
@login_required
def clips_generate(item_id: str):
    """Queue clip generation for one movie and return immediately."""
    if not _ITEM_ID.fullmatch(item_id):
        return _error("Unknown title.", 404)
    payload = request.get_json(silent=True) or {}
    refresh = bool(payload.get("refresh"))
    config = clip_analysis.LLMConfig.from_env()
    if not config.configured:
        return _error(
            "Clip generation needs a model. Add POPCORN_LLM_API_KEY to /etc/popcorn.env "
            "and restart Popcorn.", 409,
        )

    with CLIP_LOCK:
        running = _clip_jobs(active_only=True)
        mine = next((job for job in running if job.get("item_id") == item_id), None)
        if mine:
            # Clicking Generate twice must not queue the film twice.
            return jsonify({"job": _public_job(mine), "duplicate": True}), 202
        if len(running) >= CLIP_JOBS_ALLOWED:
            return _error(
                "Another movie is generating clips right now. Try again when it finishes.", 409
            )

        media, reason = _clip_media(item_id)
        if not media:
            return _error(reason or "Clips cannot be generated for this title.", 409)

        job_id = uuid.uuid4().hex
        now = time.time()
        JOBS[job_id] = {
            "id": job_id,
            "kind": "clips",
            "item_id": item_id,
            "owner_user_id": (session.get("jellyfin") or {}).get("user_id"),
            "title": media.title,
            "display_title": _display_title(media.title) or media.title,
            "status": clip_pipeline.QUEUED,
            "stage_label": clip_pipeline.STAGE_LABELS[clip_pipeline.QUEUED],
            "message": "Waiting to start.",
            "notes": [],
            "done": 0,
            "total": 0,
            "progress": 0,
            "created_at": now,
            "updated_at": now,
        }
        created = JOBS[job_id].copy()

    if refresh:
        clip_store.delete_analysis(item_id)
    _persist_job(created)
    app.logger.info("Clip job %s queued for %s", job_id, media.item_id)

    threading.Thread(
        target=_run_clip_job,
        args=(job_id, media, _load_settings(), config),
        daemon=True,
    ).start()
    return jsonify(_public_job(created)), 202


def _run_clip_job(job_id: str, media, settings: dict, config) -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="popcorn-clips-", dir=ROOT))
    _update_job(job_id, persist=True, work_dir=str(work_dir))
    hooks = _clip_hooks(job_id, work_dir)
    try:
        summary = clip_pipeline.run_clip_job(
            media=media, settings=settings, hooks=hooks,
            uploader=_ClipUploader(), subtitle_source=_ClipSubtitleSource(), config=config,
        )
        count = summary.get("clips", 0)
        _update_job(
            job_id, persist=True,
            status=clip_pipeline.COMPLETED,
            stage_label=clip_pipeline.STAGE_LABELS[clip_pipeline.COMPLETED],
            message=f"{count} clip{'s' if count != 1 else ''} ready.",
            done=count, total=count, progress=100,
            work_dir=None, finished_at=time.time(), summary=summary,
        )
    except clip_pipeline.ClipJobCancelled:
        _update_job(
            job_id, persist=True, status=clip_pipeline.CANCELLED,
            stage_label=clip_pipeline.STAGE_LABELS[clip_pipeline.CANCELLED],
            message="Stopped. Any clips already saved are still in your library.",
            work_dir=None, finished_at=time.time(),
        )
    except (clip_transcript.TranscriptionUnavailable, clip_analysis.AnalysisError,
            clip_render.RenderError) as exc:
        # These are already written for a person to read.
        _update_job(
            job_id, persist=True, status=clip_pipeline.FAILED,
            stage_label=clip_pipeline.STAGE_LABELS[clip_pipeline.FAILED],
            message=str(exc), work_dir=None, finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001 - the job must always reach a terminal state
        app.logger.exception("Clip job %s failed", job_id)
        _update_job(
            job_id, persist=True, status=clip_pipeline.FAILED,
            stage_label=clip_pipeline.STAGE_LABELS[clip_pipeline.FAILED],
            message="Clip generation stopped unexpectedly.",
            detail=f"{type(exc).__name__}: {exc}",
            work_dir=None, finished_at=time.time(),
        )
    finally:
        _remove_clip_work_dir(str(work_dir))
        with STATE_LOCK:
            JOB_PROCESSES.pop(job_id, None)


def _clip_path(record: dict) -> Path | None:
    """Where a clip lives on the library mount, if it is still there."""
    local_dir = record.get("local_dir") or ""
    file_name = record.get("file_name") or ""
    if not local_dir or not file_name:
        return None
    candidate = (LIBRARY_PATH / local_dir / file_name).resolve()
    root = LIBRARY_PATH.resolve()
    if root != candidate and root not in candidate.parents:
        return None
    return candidate


@app.get("/api/clips/stream/<clip_id>")
@login_required
def clip_stream(clip_id: str):
    """Stream a generated clip from the library mount, ranges included."""
    record = clip_store.clip(clip_id)
    if not record:
        return _error("That clip is no longer in your library.", 404)
    path = _clip_path(record)
    if not path or not path.exists():
        return _error("That clip is no longer in your library.", 404)
    download = request.args.get("download") == "1"
    name = f"{_safe_folder_name(record.get('title') or 'clip')}.mp4"
    response = send_file(
        path, mimetype="video/mp4", conditional=True,
        as_attachment=download, download_name=name,
    )
    # Clip ids are unique per generation, so a clip never changes under a URL.
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


@app.get("/api/clips/thumb/<clip_id>")
@login_required
def clip_thumb(clip_id: str):
    record = clip_store.clip(clip_id)
    if not record or not record.get("thumb_name"):
        return _error("That clip has no preview image.", 404)
    path = _clip_path({**record, "file_name": record["thumb_name"]})
    if not path or not path.exists():
        return _error("That clip has no preview image.", 404)
    response = send_file(path, mimetype="image/jpeg", conditional=True, max_age=604800)
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


_init_state()
if os.environ.get("POPCORN_JOB_RECOVERY", "1") == "1":
    threading.Thread(target=_recover_download_jobs, daemon=True, name="job-recovery").start()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
