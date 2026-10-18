import logging
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path

from local_app.config import get_config

CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def find_chrome() -> Path | None:
    configured = get_config().chrome_exe_path
    if configured and configured.exists():
        return configured

    for chrome_path in CHROME_PATHS:
        if chrome_path.exists():
            return chrome_path

    for executable_name in ("chrome.exe", "google-chrome.exe"):
        resolved = shutil.which(executable_name)
        if resolved:
            return Path(resolved)

    return None


def _is_writable_dir(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".ezmark-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def build_chrome_app_command(
    chrome_path: Path,
    url: str,
    user_data_dir: Path | None = None,
) -> list[str]:
    command = [str(chrome_path), f"--app={url}", "--new-window"]
    if user_data_dir is not None:
        command.append(f"--user-data-dir={user_data_dir}")
    return command


def open_chrome_app_mode(
    url: str,
    user_data_dir: Path | None = None,
    stderr_log_path: Path | None = None,
    startup_check_seconds: float = 1.5,
) -> bool:
    chrome_path = find_chrome()
    if chrome_path is None:
        webbrowser.open(url)
        return False

    profile_dir_exists = user_data_dir.exists() if user_data_dir is not None else None
    command = build_chrome_app_command(chrome_path, url, user_data_dir)
    logging.info("Chrome executable path: %s", chrome_path)
    if user_data_dir is not None:
        logging.info("Chrome profile dir: %s", user_data_dir)
        logging.info("Chrome profile dir exists before launch: %s", profile_dir_exists)
    logging.info("Chrome command: %s", command)
    if stderr_log_path is not None:
        logging.info("Chrome stderr log: %s", stderr_log_path)

    stderr_target = subprocess.DEVNULL
    stderr_file = None

    try:
        if user_data_dir is not None:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            logging.info("Chrome profile dir exists at launch: %s", user_data_dir.exists())
            logging.info("Chrome profile dir writable: %s", _is_writable_dir(user_data_dir))
        if stderr_log_path is not None:
            stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_file = stderr_log_path.open("ab")
            stderr_target = stderr_file

        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=stderr_target,
        )
        time.sleep(startup_check_seconds)
        exit_code = process.poll()
        if exit_code is not None:
            logging.error("Chrome exited immediately with code %s", exit_code)
            webbrowser.open(url)
            return False
        return True
    except OSError:
        logging.exception("Chrome app mode launch failed")
        webbrowser.open(url)
        return False
    finally:
        if stderr_file is not None:
            stderr_file.close()
