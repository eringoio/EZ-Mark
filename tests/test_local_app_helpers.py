import json
import socket
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

import local_app.browser as browser
import local_app.health as health_module
import local_app.launcher as launcher
from local_app.config import get_config, parse_local_env
from local_app.health import check_health
from local_app.paths import ensure_runtime_dirs
from local_app.process_manager import build_server_env, is_port_open


def test_launcher_config_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZMARK_ROOT", r"C:\Temp\EZMarkTest")
    monkeypatch.setenv("EZMARK_HOST", "127.0.0.2")
    monkeypatch.setenv("EZMARK_PORT", "8010")
    monkeypatch.setenv("EZMARK_SERVER_EXE", r"C:\Temp\EZMarkTest\custom-server.exe")
    monkeypatch.setenv("EZMARK_DEV_SERVER_CMD", r"python scripts\run_ezmark_server.py")

    config = get_config()

    assert config.root_dir == Path(r"C:\Temp\EZMarkTest").resolve()
    assert config.host == "127.0.0.2"
    assert config.port == 8010
    assert config.base_url == "http://127.0.0.2:8010"
    assert config.health_url == "http://127.0.0.2:8010/healthz"
    assert config.server_exe_path == Path(r"C:\Temp\EZMarkTest\custom-server.exe")
    assert config.data_dir == config.root_dir / "data"
    assert config.logs_dir == config.root_dir / "logs"
    assert config.config_dir == config.root_dir / "config"
    assert config.database_path == config.root_dir / "data" / "app.sqlite"
    assert config.database_url == "sqlite:///" + config.database_path.as_posix()
    assert config.dev_server_cmd == r"python scripts\run_ezmark_server.py"


def test_launcher_config_server_exe_defaults_to_root_server(monkeypatch) -> None:
    monkeypatch.setenv("EZMARK_ROOT", r"C:\Temp\EZMarkTest")
    monkeypatch.delenv("EZMARK_SERVER_EXE", raising=False)

    config = get_config()

    assert config.server_exe_path == config.root_dir / "server" / "ezmark-server.exe"


def test_launcher_config_uses_repo_root_in_development(monkeypatch) -> None:
    monkeypatch.delenv("EZMARK_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    config = get_config()

    assert config.root_dir == Path(__file__).resolve().parents[1]
    assert config.server_exe_path == config.root_dir / "server" / "ezmark-server.exe"


def test_launcher_config_uses_executable_parent_when_frozen(tmp_path, monkeypatch) -> None:
    fake_exe = tmp_path / "EZMark.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.delenv("EZMARK_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    config = get_config()

    # Binaries stay beside the executable; persistent data/logs/config/backups
    # default to the writable per-user EZ Mark location (see Prompt 07).
    assert config.root_dir == tmp_path
    assert config.data_dir == tmp_path / "LocalAppData" / "EZMark" / "data"
    assert config.logs_dir == tmp_path / "LocalAppData" / "EZMark" / "logs"
    assert config.config_dir == tmp_path / "LocalAppData" / "EZMark" / "config"
    assert config.backups_dir == tmp_path / "LocalAppData" / "EZMark" / "backups"
    assert config.server_exe_path == tmp_path / "server" / "ezmark-server.exe"


def test_launcher_config_portable_install_keeps_data_beside_exe(tmp_path, monkeypatch) -> None:
    fake_exe = tmp_path / "EZMark.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("EZMARK_ROOT", str(tmp_path / "Portable"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    config = get_config()

    # Explicit EZMARK_ROOT = deliberate portable install: everything beside the exe.
    assert config.root_dir == (tmp_path / "Portable").resolve()
    assert config.data_dir == (tmp_path / "Portable") / "data"
    assert config.config_dir == (tmp_path / "Portable") / "config"
    assert config.backups_dir == (tmp_path / "Portable") / "backups"


def test_launcher_chrome_profile_uses_localappdata_on_windows(tmp_path, monkeypatch) -> None:
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("EZMARK_ROOT", str(tmp_path / "InstallRoot"))

    config = get_config()

    assert config.chrome_profile_dir == local_app_data / "EZMark" / "chrome-profile"
    assert config.chrome_logs_dir == local_app_data / "EZMark" / "logs"
    assert config.chrome_launch_log_path == local_app_data / "EZMark" / "logs" / "chrome-launch.log"
    assert config.chrome_profile_dir != config.root_dir / "chrome-profile"


def test_launcher_chrome_profile_uses_windows_home_fallback(tmp_path, monkeypatch) -> None:
    home = tmp_path / "Home"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("EZMARK_ROOT", str(tmp_path / "InstallRoot"))

    assert get_config().chrome_profile_dir == home / "AppData" / "Local" / "EZMark" / "chrome-profile"


def test_launcher_chrome_profile_uses_xdg_data_home_on_non_windows(tmp_path, monkeypatch) -> None:
    xdg_data_home = tmp_path / "xdg-data"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data_home))
    monkeypatch.setenv("EZMARK_ROOT", str(tmp_path / "InstallRoot"))

    config = get_config()

    assert config.chrome_profile_dir == xdg_data_home / "ezmark" / "chrome-profile"
    assert config.chrome_profile_dir != config.root_dir / "chrome-profile"


def test_server_command_dev_mode_uses_repo_root_as_cwd(monkeypatch) -> None:
    runtime_root = Path(r"C:\Temp\EZMarkRuntime")
    repo_root = Path(launcher.__file__).resolve().parents[1]
    monkeypatch.setenv("EZMARK_ROOT", str(runtime_root))
    monkeypatch.setenv("EZMARK_DEV_SERVER_CMD", r"python scripts\run_ezmark_server.py")

    command, cwd = launcher._server_command()

    assert command == ["python", r"scripts\run_ezmark_server.py"]
    assert cwd == repo_root
    assert cwd != runtime_root


def test_server_command_dev_mode_preserves_absolute_command(monkeypatch) -> None:
    repo_root = Path(launcher.__file__).resolve().parents[1]
    absolute_script = repo_root / "scripts" / "run_ezmark_server.py"
    monkeypatch.setenv("EZMARK_ROOT", str(repo_root / ".runtime"))
    monkeypatch.setenv("EZMARK_DEV_SERVER_CMD", f'python "{absolute_script}"')

    command, cwd = launcher._server_command()

    assert command == ["python", str(absolute_script)]
    assert cwd == repo_root


def test_server_command_production_mode_uses_app_root_as_cwd(tmp_path, monkeypatch) -> None:
    app_root = tmp_path / "MovedEZMark"
    server_exe = app_root / "server" / "ezmark-server.exe"
    server_exe.parent.mkdir(parents=True)
    server_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("EZMARK_ROOT", str(app_root))
    monkeypatch.delenv("EZMARK_DEV_SERVER_CMD", raising=False)
    monkeypatch.delenv("EZMARK_SERVER_EXE", raising=False)

    command, cwd = launcher._server_command()
    config = get_config()

    assert command == [str(server_exe)]
    assert cwd == app_root.resolve()
    assert config.data_dir == app_root.resolve() / "data"
    assert config.logs_dir == app_root.resolve() / "logs"
    assert config.config_dir == app_root.resolve() / "config"
    assert str(config.root_dir).startswith(str(tmp_path))


def test_startup_timeout_defaults_to_90_seconds(monkeypatch) -> None:
    monkeypatch.delenv("EZMARK_STARTUP_TIMEOUT_SECONDS", raising=False)

    assert launcher._startup_timeout_seconds() == 90


def test_startup_timeout_uses_valid_env_override(monkeypatch) -> None:
    monkeypatch.setenv("EZMARK_STARTUP_TIMEOUT_SECONDS", "120")

    assert launcher._startup_timeout_seconds() == 120


def test_startup_timeout_rejects_invalid_or_too_low_values(monkeypatch) -> None:
    monkeypatch.setenv("EZMARK_STARTUP_TIMEOUT_SECONDS", "invalid")
    assert launcher._startup_timeout_seconds() == 90

    monkeypatch.setenv("EZMARK_STARTUP_TIMEOUT_SECONDS", "30")
    assert launcher._startup_timeout_seconds() == 90


def test_show_error_prints_without_message_box_on_non_windows(monkeypatch, capsys) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "linux")

    launcher._show_error("startup failed")

    assert capsys.readouterr().out == "startup failed\n"


def test_show_error_logs_warning_when_windows_message_box_fails(
    monkeypatch, capsys, caplog
) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ctypes", None)

    launcher._show_error("startup failed")

    assert capsys.readouterr().out == "startup failed\n"
    assert "Could not show Windows message box" in caplog.text


class _FakeProcess:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_wait_for_server_health_final_check_passes_when_process_alive(monkeypatch) -> None:
    health_results = [True]
    monotonic_values = iter([0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launcher, "check_health", lambda _url: health_results.pop(0))

    assert launcher._wait_for_server_health(
        "http://127.0.0.1:8000/healthz",
        _FakeProcess(None),
        timeout_seconds=1,
    )


def test_wait_for_server_health_progress_callback_reports_ready(monkeypatch) -> None:
    health_results = [True]
    progress = []
    monotonic_values = iter([0.0, 0.1, 0.1])
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launcher, "check_health", lambda _url: health_results.pop(0))

    assert launcher._wait_for_server_health(
        "http://127.0.0.1:8000/healthz",
        _FakeProcess(None),
        timeout_seconds=1,
        progress_callback=progress.append,
    )
    assert progress == ["almost"]


def test_wait_for_server_health_does_not_final_check_dead_process(monkeypatch) -> None:
    health_checks = []
    monotonic_values = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        launcher,
        "check_health",
        lambda _url: health_checks.append(_url) or False,
    )

    assert not launcher._wait_for_server_health(
        "http://127.0.0.1:8000/healthz",
        _FakeProcess(1),
        timeout_seconds=1,
    )
    assert health_checks == []


def test_ensure_runtime_dirs_uses_configured_root(tmp_path, monkeypatch) -> None:
    root_dir = tmp_path / "EZMark"
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("EZMARK_ROOT", str(root_dir))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    existing_file = root_dir / "data" / "keep.txt"
    existing_file.parent.mkdir(parents=True)
    existing_file.write_text("do not delete", encoding="utf-8")

    ensure_runtime_dirs()

    assert root_dir.exists()
    assert (root_dir / "data").exists()
    assert (root_dir / "logs").exists()
    assert (root_dir / "config").exists()
    assert (root_dir / "backups").exists()
    assert not (root_dir / "chrome-profile").exists()
    assert (local_app_data / "EZMark" / "chrome-profile").exists()
    assert (local_app_data / "EZMark" / "logs").exists()
    assert existing_file.read_text(encoding="utf-8") == "do not delete"


def test_build_server_env_uses_local_runtime_paths(tmp_path) -> None:
    data_dir = tmp_path / "runtime root" / "data"
    logs_dir = tmp_path / "runtime root" / "logs"
    env = build_server_env("127.0.0.1", 8000, data_dir, logs_dir=logs_dir)

    assert env["HOST"] == "127.0.0.1"
    assert env["PORT"] == "8000"
    assert env["DATABASE_URL"] == "sqlite:///" + (data_dir / "app.sqlite").as_posix()
    assert env["DATA_ROOT"] == data_dir.as_posix()
    assert env["ENV"] == "production"
    assert env["LOG_LEVEL"] == "info"
    assert env["LOG_PATH"] == (logs_dir / "server.log").as_posix()


def test_parse_local_env_ignores_comments_and_blank_lines(tmp_path) -> None:
    env_path = tmp_path / "local.env"
    env_path.write_text(
        "\n"
        "# comment\n"
        "ENV=production\n"
        "LOG_LEVEL=debug\n"
        "SPACED = value with spaces \n"
        "ignored-without-equals\n"
        "=missing-key\n",
        encoding="utf-8",
    )

    assert parse_local_env(env_path) == {
        "ENV": "production",
        "LOG_LEVEL": "debug",
        "SPACED": "value with spaces",
    }


def test_build_server_env_dynamic_values_override_local_env(tmp_path) -> None:
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    local_env = {
        "HOST": "0.0.0.0",
        "PORT": "9999",
        "DATABASE_URL": "sqlite:///wrong/app.sqlite",
        "DATA_ROOT": "wrong-data",
        "ENV": "development",
        "LOG_PATH": "wrong.log",
        "LOG_LEVEL": "debug",
        "MAX_UPLOAD_SIZE_MB": "100",
    }

    env = build_server_env("127.0.0.1", 8000, data_dir, "info", logs_dir, local_env)

    assert env["HOST"] == "127.0.0.1"
    assert env["PORT"] == "8000"
    assert env["DATABASE_URL"] == "sqlite:///" + (data_dir / "app.sqlite").as_posix()
    assert env["DATA_ROOT"] == data_dir.as_posix()
    assert env["ENV"] == "production"
    assert env["LOG_PATH"] == (logs_dir / "server.log").as_posix()
    assert env["LOG_LEVEL"] == "info"
    assert env["MAX_UPLOAD_SIZE_MB"] == "100"


def test_check_health_returns_false_for_unavailable_endpoint() -> None:
    assert check_health("http://127.0.0.1:9/healthz", timeout_seconds=0.1) is False


class _FakeResponse:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_check_health_returns_true_for_ok_status(monkeypatch) -> None:
    monkeypatch.setattr(
        health_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeResponse(
            200,
            json.dumps({"status": "ok", "app": "ezmark"}).encode(),
        ),
    )

    assert check_health("http://127.0.0.1:8000/healthz") is True


def test_check_health_rejects_ok_status_without_app(monkeypatch) -> None:
    monkeypatch.setattr(
        health_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeResponse(200, json.dumps({"status": "ok"}).encode()),
    )

    assert check_health("http://127.0.0.1:8000/healthz") is False


def test_check_health_returns_false_for_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        health_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeResponse(200, b"not-json"),
    )

    assert check_health("http://127.0.0.1:8000/healthz") is False


def test_check_health_returns_false_for_non_ok_status(monkeypatch) -> None:
    monkeypatch.setattr(
        health_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeResponse(200, json.dumps({"status": "starting"}).encode()),
    )

    assert check_health("http://127.0.0.1:8000/healthz") is False


def test_check_health_returns_false_for_timeout(monkeypatch) -> None:
    def raise_timeout(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(health_module, "urlopen", raise_timeout)

    assert check_health("http://127.0.0.1:8000/healthz") is False


def test_check_health_returns_false_for_connection_error(monkeypatch) -> None:
    def raise_connection_error(*_args, **_kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(health_module, "urlopen", raise_connection_error)

    assert check_health("http://127.0.0.1:8000/healthz") is False


def test_find_chrome_uses_explicit_path(tmp_path, monkeypatch) -> None:
    chrome_path = tmp_path / "chrome.exe"
    chrome_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("EZMARK_CHROME_EXE", str(chrome_path))

    assert browser.find_chrome() == chrome_path


def test_open_chrome_app_mode_falls_back_to_default_browser(tmp_path, monkeypatch) -> None:
    opened_urls = []
    monkeypatch.delenv("EZMARK_CHROME_EXE", raising=False)
    monkeypatch.setattr(browser, "CHROME_PATHS", (tmp_path / "missing-chrome.exe",))
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)
    monkeypatch.setattr(browser.webbrowser, "open", lambda url: opened_urls.append(url))

    result = browser.open_chrome_app_mode("http://127.0.0.1:8000")

    assert result is False
    assert opened_urls == ["http://127.0.0.1:8000"]


def test_open_chrome_app_mode_redirects_stderr_and_reports_success(tmp_path, monkeypatch) -> None:
    chrome_path = tmp_path / "chrome.exe"
    chrome_path.write_text("", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    stderr_log = tmp_path / "logs" / "chrome-launch.log"
    popen_calls = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, stdout, stderr):
        popen_calls.append((command, stdout, stderr))
        return FakeProcess()

    monkeypatch.setattr(browser, "find_chrome", lambda: chrome_path)
    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)

    result = browser.open_chrome_app_mode(
        "http://127.0.0.1:8000",
        user_data_dir=profile_dir,
        stderr_log_path=stderr_log,
    )

    assert result is True
    assert stderr_log.exists()
    assert popen_calls[0][0] == [
        str(chrome_path),
        "--app=http://127.0.0.1:8000",
        "--new-window",
        f"--user-data-dir={profile_dir}",
    ]
    assert popen_calls[0][1] == subprocess.DEVNULL


def test_open_chrome_app_mode_falls_back_when_chrome_exits_immediately(
    tmp_path, monkeypatch
) -> None:
    chrome_path = tmp_path / "chrome.exe"
    chrome_path.write_text("", encoding="utf-8")
    opened_urls = []

    class FakeProcess:
        def poll(self):
            return 7

    monkeypatch.setattr(browser, "find_chrome", lambda: chrome_path)
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(browser.webbrowser, "open", lambda url: opened_urls.append(url))

    result = browser.open_chrome_app_mode(
        "http://127.0.0.1:8000",
        user_data_dir=tmp_path / "profile",
        stderr_log_path=tmp_path / "logs" / "chrome-launch.log",
    )

    assert result is False
    assert opened_urls == ["http://127.0.0.1:8000"]


def test_is_port_open_detects_open_and_closed_ports() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(1)
        host, port = server_socket.getsockname()

        assert is_port_open(host, port) is True

    assert is_port_open("127.0.0.1", port) is False
