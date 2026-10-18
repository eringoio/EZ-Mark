import sys
import types
from pathlib import Path

from scripts import run_ezmark_server


def test_ensure_repo_root_on_path_inserts_repo_root(monkeypatch):
    repo_root = Path(run_ezmark_server.__file__).resolve().parents[1]
    original_path = [entry for entry in sys.path if entry != str(repo_root)]
    monkeypatch.setattr(sys, "path", original_path)

    assert run_ezmark_server._ensure_repo_root_on_path() == repo_root
    assert sys.path[0] == str(repo_root)


def test_ensure_repo_root_on_path_skips_path_mutation_when_frozen(monkeypatch):
    repo_root = Path(run_ezmark_server.__file__).resolve().parents[1]
    original_path = [entry for entry in sys.path if entry != str(repo_root)]
    monkeypatch.setattr(sys, "path", original_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert run_ezmark_server._ensure_repo_root_on_path() == repo_root
    assert str(repo_root) not in sys.path


def test_main_uses_env_and_disables_reload(monkeypatch):
    calls = []
    fastapi_app = object()
    fake_app_main = types.ModuleType("app.main")
    fake_app_main.app = fastapi_app

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setitem(sys.modules, "app.main", fake_app_main)
    monkeypatch.setattr(run_ezmark_server.uvicorn, "run", fake_run)
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    run_ezmark_server.main()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (fastapi_app,)
    assert kwargs == {
        "host": "0.0.0.0",
        "port": 8123,
        "log_level": "debug",
        "reload": False,
    }
