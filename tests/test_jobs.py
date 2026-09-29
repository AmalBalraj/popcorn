import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict

import pytest

from job_transfer import Transfer, stop_transfer, transfer_state
from sources.models import Release

JOB_ID = "b" * 32
HASH = "a" * 40


def make_job(web, **overrides):
    directory = web.ROOT / "popcorn-web-test"
    directory.mkdir(exist_ok=True)
    (directory / "Example.mp4").write_bytes(b"verified media fixture")
    result = Release("Example 2020 1080p", infohash=HASH, source="tpb")
    job = {
        "id": JOB_ID,
        "title": result.title,
        "release": asdict(result),
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "downloading",
        "destination": "test:Movies",
        "download_dir": str(directory),
        "owner_user_id": "owner",
        "download_complete": False,
        "upload_complete": False,
        **overrides,
    }
    web.JOBS[JOB_ID] = job
    web._persist_job(job, required=True)
    return result, directory


def fake_transfers(monkeypatch, web, codes):
    commands = []

    class FakeTransfer:
        pid = None

        def __init__(self, directory, command):
            commands.append((str(directory), command))
            self.code = codes.pop(0)

        def poll(self):
            return self.code

        def wait(self):
            return self.code

    monkeypatch.setattr(web, "Transfer", FakeTransfer)
    return commands


def test_scenario_g_upload_retry_never_redownloads(web, monkeypatch):
    result, directory = make_job(web)
    commands = fake_transfers(monkeypatch, web, [0, 1, 0])
    web._run_download(JOB_ID, result, "test:Movies", None)
    assert web.JOBS[JOB_ID]["status"] == "uploading"
    assert web.JOBS[JOB_ID]["download_complete"]
    assert directory.exists() and (directory / "Example.mp4").exists()
    assert web.JOBS[JOB_ID]["recoverable"]
    assert web.JOBS[JOB_ID]["retry_at"] > time.time()
    # Simulate a restart by reloading exclusively from SQLite.
    web.JOBS.clear()
    web._init_state()
    web._run_download(JOB_ID, result, "test:Movies", None)
    assert web.JOBS[JOB_ID]["status"] == "complete"
    assert [cmd[0] for _, cmd in commands] == ["aria2c", "rclone", "rclone"]
    assert not directory.exists()


def test_scenario_h_restart_keeps_original_directory_and_state(web, monkeypatch):
    result, directory = make_job(web, resolved_magnet=f"magnet:?xt=urn:btih:{HASH}")
    control = directory / "Example.mp4.aria2"
    control.write_bytes(b"saved pieces")
    web.JOBS.clear()
    web._init_state()
    assert web.JOBS[JOB_ID]["status"] == "downloading"
    assert control.read_bytes() == b"saved pieces"
    commands = fake_transfers(monkeypatch, web, [1])
    web._run_download(JOB_ID, result, "test:Movies", None)
    assert commands[0][0] == str(directory)
    assert f"--dir={directory}" in commands[0][1]
    assert "--continue=true" in commands[0][1]
    assert "--check-integrity=true" in commands[0][1]
    assert control.exists()
    assert web.JOBS[JOB_ID]["status"] == "downloading"


def test_indexing_retry_never_repeats_transfer_or_upload(web, monkeypatch):
    result, directory = make_job(
        web,
        download_complete=True,
        upload_complete=True,
        status="indexing",
        destination=web.LIBRARY_REMOTE,
        media_paths=["/library/Example.mp4"],
    )
    commands = fake_transfers(monkeypatch, web, [])
    monkeypatch.setattr(web, "_refresh_library_mount", lambda: None)
    monkeypatch.setattr(web, "_find_library_item", lambda *args: None)
    monkeypatch.setattr(web, "_refresh_library", lambda *args: False)
    web._run_download(JOB_ID, result, web.LIBRARY_REMOTE, None)
    assert web.JOBS[JOB_ID]["status"] == "indexing"
    assert not commands
    monkeypatch.setattr(web, "_find_library_item", lambda *args: "library-id")

    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(web.threading, "Timer", Timer)
    web._run_download(JOB_ID, result, web.LIBRARY_REMOTE, None)
    assert web.JOBS[JOB_ID]["status"] == "complete"
    assert web.JOBS[JOB_ID]["library_item_id"] == "library-id"
    assert not commands


def test_legacy_download_migration_retains_partial_files(web):
    result, directory = make_job(web)
    web.JOBS[JOB_ID].pop("release")
    web._persist_job(web.JOBS[JOB_ID])
    web.JOBS.clear()
    web._init_state()
    assert web.JOBS[JOB_ID]["status"] == "interrupted"
    assert directory.exists()


def test_completion_checkpoint_is_not_visible_when_database_fails(web, monkeypatch):
    result, directory = make_job(web)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(web, "_persist_job", fail)
    with pytest.raises(sqlite3.Error):
        web._set_job(JOB_ID, download_complete=True)
    assert not web.JOBS[JOB_ID]["download_complete"]
    assert directory.exists()


def test_cancel_finished_download_keeps_only_local_copy(web):
    result, directory = make_job(web, download_complete=True, cancel_requested=True)
    web._run_download(JOB_ID, result, "test:Movies", None)
    assert web.JOBS[JOB_ID]["status"] == "cancelled"
    assert directory.exists()


def test_recovery_loads_active_jobs_beyond_old_history_limit(web):
    result, directory = make_job(web, created_at=1)
    for n in range(205):
        now = time.time()
        web._persist_job(
            {
                "id": f"{n:032x}",
                "status": "complete",
                "created_at": now,
                "updated_at": now,
            }
        )
    web.JOBS.clear()
    web._init_state()
    assert JOB_ID in web.JOBS and web.JOBS[JOB_ID]["status"] == "downloading"


def signed_client(web, user="owner", admin=False):
    web.app.config.update(TESTING=True)
    client = web.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["jellyfin"] = {"user_id": user, "is_admin": admin, "username": user}
    return client


def test_admin_diagnostics_and_job_redaction(web, monkeypatch):
    result, directory = make_job(web, resolved_magnet="secret-magnet")
    user = signed_client(web)
    assert user.get("/api/admin/providers").status_code == 403
    admin = signed_client(web, admin=True)
    response = admin.get("/api/admin/providers")
    assert response.status_code == 200
    assert "https://" not in response.get_data(as_text=True)
    data = user.get(f"/api/jobs/{JOB_ID}").get_json()
    assert (
        "release" not in data
        and "resolved_magnet" not in data
        and "download_dir" not in data
    )
    assert (
        signed_client(web, user="someone-else")
        .post(f"/api/jobs/{JOB_ID}/retry")
        .status_code
        == 404
    )


def test_retry_endpoint_resumes_existing_job(web, monkeypatch):
    result, directory = make_job(
        web, download_complete=True, status="uploading", recoverable=True
    )
    launches = []
    monkeypatch.setattr(web, "_start_download_job", lambda *args: launches.append(args))
    response = signed_client(web).post(f"/api/jobs/{JOB_ID}/retry")
    assert response.status_code == 202 and launches[0][0] == JOB_ID
    assert web.JOBS[JOB_ID]["download_complete"] and directory.exists()


def test_api_survives_all_provider_failures(web, monkeypatch):
    from sources.service import SearchResult

    monkeypatch.setattr(
        web.popcorn.source_service,
        "search",
        lambda *a, **k: SearchResult(
            "search-id", [], {"tpb": {"error": "parser", "available": False}}
        ),
    )
    response = signed_client(web).post("/api/search", json={"query": "Example"})
    assert response.status_code == 200 and response.get_json()["count"] == 0


def test_detached_transfer_reattaches_after_parent_exits(tmp_path):
    # A real supervisor and child process, not a mocked Popen. The initial
    # web-like parent exits while the transfer remains running.
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import time; p=Path('runs'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); time.sleep(0.5)",
    ]
    command = [
        sys.executable,
        "-c",
        f"import os; os.chdir({str(tmp_path)!r}); " + command[2],
    ]
    driver = f"from job_transfer import Transfer; t=Transfer({str(tmp_path)!r}, {command!r}); print(t.pid, flush=True)"
    parent = subprocess.run(
        [sys.executable, "-c", driver], capture_output=True, text=True, timeout=5
    )
    assert parent.returncode == 0, parent.stderr
    transfer = Transfer(tmp_path, command)
    assert transfer.pid == int(parent.stdout.strip())
    assert transfer.wait() == 0
    assert (tmp_path / "runs").read_text() == "x"
    assert transfer_state(tmp_path)["exit_code"] == 0
    # Crash after transfer success but before SQLite checkpoint: do not rerun.
    assert Transfer(tmp_path, command).wait() == 0
    assert (tmp_path / "runs").read_text() == "x"


def test_stopped_supervisor_can_resume_same_local_state(tmp_path):
    partial = tmp_path / "movie.aria2"
    partial.write_bytes(b"partial")
    transfer = Transfer(tmp_path, [sys.executable, "-c", "import time; time.sleep(5)"])
    stop_transfer(tmp_path)
    assert transfer.wait() != 0 and partial.read_bytes() == b"partial"
    resumed = Transfer(tmp_path, [sys.executable, "-c", "pass"])
    assert resumed.wait() == 0 and partial.read_bytes() == b"partial"


def test_live_aria_progress_still_updates_frontend_fields(web, monkeypatch):
    result, directory = make_job(web)

    class ProgressTransfer:
        pid = None
        checks = 0

        def __init__(self, *args):
            pass

        def poll(self):
            self.checks += 1
            return None if self.checks <= 2 else 1

        def wait(self):
            return 1

        def progress(self):
            return "[#abc 12MiB/100MiB(12%) CN:4 SD:2 DL:3.2MiB ETA:28s]"

    monkeypatch.setattr(web, "Transfer", ProgressTransfer)
    web._run_download(JOB_ID, result, "test:Movies", None)
    saved = web.JOBS[JOB_ID]
    assert saved["progress"] == 12
    assert saved["downloaded"] == "12MiB" and saved["total"] == "100MiB"


def test_existing_single_provider_preference_keeps_web_fallbacks(web, monkeypatch):
    from sources.service import SearchResult

    seen = []
    monkeypatch.setattr(
        web,
        "_load_settings",
        lambda: {"default_source": "regional", "search_timeout": 15},
    )

    def search(context, names, **kwargs):
        seen.append((names, kwargs))
        return SearchResult("search", [], {})

    monkeypatch.setattr(web.popcorn.source_service, "search", search)
    client = signed_client(web)
    assert client.post("/api/search", json={"query": "Example"}).status_code == 200
    assert set(seen[-1][0]) == set(web.popcorn.source_service.providers)
    assert seen[-1][1]["preferred"] == "regional"
    assert (
        client.post(
            "/api/search", json={"query": "Example", "source": "regional"}
        ).status_code
        == 200
    )
    assert seen[-1][0] == ["regional"]


def test_transfer_logs_keep_numeric_progress_and_discard_credentials(tmp_path):
    command = [
        sys.executable,
        "-c",
        "print('failed URL https://user:secret@example.invalid/?apikey=secret'); print('[12MiB/100MiB(12%) CN:2 DL:3MiB ETA:2s]')",
    ]
    transfer = Transfer(tmp_path, command)
    assert transfer.wait() == 0
    logged = transfer.progress()
    assert "12MiB/100MiB(12%)" in logged
    assert "secret" not in logged and "example.invalid" not in logged
