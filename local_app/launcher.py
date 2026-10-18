import logging
import os
import shlex
import sys
import time
from pathlib import Path

from local_app.browser import find_chrome, open_chrome_app_mode
from local_app.config import get_config, parse_local_env
from local_app.health import check_health
from local_app.paths import ensure_runtime_dirs
from local_app.process_manager import build_server_env, is_port_open, start_server_command
from local_app.splash import create_startup_splash, startup_messages

DEFAULT_STARTUP_TIMEOUT_SECONDS = 90
MIN_STARTUP_TIMEOUT_SECONDS = 90


def _configure_logging(log_path: Path) -> None:
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _show_error(message: str) -> None:
    print(message)
    if sys.platform != "win32":
        return

    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "EZ Mark", 0x10)
    except Exception:
        logging.warning("Could not show Windows message box", exc_info=True)


def _split_server_command(command: str) -> list[str]:
    return [
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
        for part in shlex.split(command, posix=os.name != "nt")
    ]


def _startup_timeout_seconds() -> int:
    raw_value = os.getenv("EZMARK_STARTUP_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_STARTUP_TIMEOUT_SECONDS

    try:
        timeout = int(raw_value)
    except ValueError:
        return DEFAULT_STARTUP_TIMEOUT_SECONDS

    if timeout < MIN_STARTUP_TIMEOUT_SECONDS:
        return DEFAULT_STARTUP_TIMEOUT_SECONDS
    return timeout


def _server_command() -> tuple[list[str], Path] | None:
    config = get_config()
    if config.dev_server_cmd:
        repo_root = Path(__file__).resolve().parents[1]
        return _split_server_command(config.dev_server_cmd), repo_root

    if config.server_exe_path.exists():
        return [str(config.server_exe_path)], config.root_dir

    return None


def _open_browser(url: str) -> None:
    config = get_config()
    chrome_path = find_chrome()
    logging.info("Chrome path: %s", chrome_path if chrome_path else "not found")
    chrome_used = open_chrome_app_mode(
        url,
        user_data_dir=config.chrome_profile_dir,
        stderr_log_path=config.chrome_launch_log_path,
    )
    if chrome_used:
        logging.info("Opened Chrome app mode at %s", url)
    else:
        message = (
            "EZ Mark server is running, but Chrome app mode could not be opened. "
            f"Please open {url}/ manually."
        )
        logging.warning(message)
        _show_error(message)


def _wait_for_server_health(
    health_url: str,
    process,
    timeout_seconds: int,
    interval_seconds: float = 0.5,
    progress_seconds: int = 5,
    progress_callback=None,
) -> bool:
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    next_progress_at = started_at + progress_seconds

    logging.info("Waiting for health check at %s", health_url)
    while time.monotonic() < deadline:
        if check_health(health_url):
            elapsed = time.monotonic() - started_at
            logging.info("Health check OK after %.1f seconds", elapsed)
            logging.info("Total startup wait time: %.1f seconds", elapsed)
            if progress_callback is not None:
                progress_callback("almost")
            return True

        now = time.monotonic()
        if now >= next_progress_at:
            elapsed = now - started_at
            logging.info(
                "Still waiting for health check after %.1f seconds of %s seconds",
                elapsed,
                timeout_seconds,
            )
            if progress_callback is not None:
                progress_callback("loading")
            next_progress_at = now + progress_seconds
        time.sleep(interval_seconds)

    elapsed = time.monotonic() - started_at
    logging.warning("Health wait timed out after %.1f seconds", elapsed)

    if process is not None and process.poll() is None:
        logging.info("Server process is still alive; running final health check")
        if check_health(health_url):
            elapsed = time.monotonic() - started_at
            logging.info("Final health check OK after %.1f seconds", elapsed)
            logging.info("Total startup wait time: %.1f seconds", elapsed)
            if progress_callback is not None:
                progress_callback("almost")
            return True
        logging.warning("Final health check did not pass")
    elif process is not None:
        logging.error("Server process exited before health became OK with code %s", process.poll())

    logging.info("Total startup wait time: %.1f seconds", elapsed)
    return False


def main() -> int:
    config = get_config()
    ensure_runtime_dirs()

    launcher_log_path = config.logs_dir / "launcher.log"
    server_log_path = config.logs_dir / "server.log"
    _configure_logging(launcher_log_path)

    logging.info("Starting EZ Mark launcher")
    logging.info("Root path: %s", config.root_dir)
    logging.info("Server exe path: %s", config.server_exe_path)
    logging.info("Checking health at %s", config.health_url)
    startup_timeout_seconds = _startup_timeout_seconds()
    logging.info("Startup timeout: %s seconds", startup_timeout_seconds)

    if check_health(config.health_url):
        logging.info("Health check result: healthy")
        logging.info("Existing EZ Mark server found")
        server_started_by_launcher = False
        logging.info("server_started_by_launcher=%s", server_started_by_launcher)
        _open_browser(config.base_url)
        return 0

    logging.info("Health check result: not healthy")

    if is_port_open(config.host, config.port):
        message = (
            f"EZ Mark could not start because port {config.port} is already in use.\n\n"
            "Another program may already be using the port.\n\n"
            "Please close the other program or ask the system administrator to check the EZ Mark logs."
        )
        logging.error(message)
        _show_error(message)
        return 1

    server_command = _server_command()
    if server_command is None:
        message = (
            "EZ Mark could not start because the local server file was not found.\n\n"
            f"Expected:\n{config.server_exe_path}\n\n"
            "Please check the installation folder.\n\n"
            "For development testing, set EZMARK_DEV_SERVER_CMD explicitly, for example:\n"
            "python scripts\\run_ezmark_server.py\n\n"
            f"Log file:\n{launcher_log_path}"
        )
        logging.error(message)
        _show_error(message)
        return 1
    command, server_cwd = server_command

    local_env = parse_local_env(config.local_env_path)
    env = build_server_env(
        config.host,
        config.port,
        config.data_dir,
        config.log_level,
        config.logs_dir,
        local_env=local_env,
    )
    logging.info("Starting server command: %s", command)
    logging.info("Server cwd: %s", server_cwd)
    messages = startup_messages("ja")
    splash = create_startup_splash("ja")
    splash.update(messages["starting"])
    process = start_server_command(command, env, server_log_path, cwd=server_cwd)

    try:
        if _wait_for_server_health(
            config.health_url,
            process,
            timeout_seconds=startup_timeout_seconds,
            interval_seconds=0.5,
            progress_callback=lambda key: splash.update(messages.get(key, key)),
        ):
            splash.update(messages["almost"])
            server_started_by_launcher = True
            logging.info("EZ Mark server became healthy")
            logging.info("server_started_by_launcher=%s", server_started_by_launcher)
            splash.close()
            _open_browser(config.base_url)
            return 0
    finally:
        splash.close()

    message = (
        "EZ Mark could not start the local server.\n\n"
        f"Please check:\n{server_log_path}\n{launcher_log_path}"
    )
    logging.error(message)
    _show_error(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
