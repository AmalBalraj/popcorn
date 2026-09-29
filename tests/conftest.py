import importlib
import tempfile
import pytest


@pytest.fixture
def web(tmp_path, monkeypatch):
    # Import-time recovery must not inspect or modify the operator's jobs.
    with tempfile.TemporaryDirectory(prefix="popcorn-test-import-") as initial:
        monkeypatch.setenv("POPCORN_STATE_FILE", initial + "/state.sqlite3")
        monkeypatch.setenv("POPCORN_JOB_RECOVERY", "0")
        module = importlib.import_module("web_app")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "data/state.sqlite3")
    monkeypatch.setattr(module, "SETTINGS_FILE", tmp_path / "data/settings.json")
    monkeypatch.setattr(module, "JOBS", {})
    monkeypatch.setattr(module, "JOB_WORKERS", set())
    monkeypatch.setattr(module, "JOB_PROCESSES", {})
    monkeypatch.setattr(module, "TMDB_API_KEY", "")
    module._init_state()
    return module
