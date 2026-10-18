import os
import socket
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def is_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def build_server_env(
    host: str,
    port: int,
    data_dir: Path,
    log_level: str = "info",
    logs_dir: Path | None = None,
    local_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    if local_env:
        env.update(local_env)
    database_path = data_dir / "app.sqlite"
    logs_root = logs_dir if logs_dir is not None else data_dir.parent / "logs"
    env.update(
        {
            "HOST": host,
            "PORT": str(port),
            "DATABASE_URL": "sqlite:///" + database_path.as_posix(),
            "DATA_ROOT": data_dir.as_posix(),
            "ENV": "production",
            "LOG_LEVEL": log_level,
            "LOG_PATH": str((logs_root / "server.log").as_posix()),
        }
    )
    return env


def start_server_command(
    command: Sequence[str],
    env: dict[str, str],
    log_path: Path,
    cwd: Path | None = None,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        list(command),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=cwd,
        creationflags=creationflags,
    )


def start_server_process(
    server_exe_path: Path,
    env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen:
    return start_server_command([str(server_exe_path)], env, log_path, cwd=server_exe_path.parent)


def terminate_process(process: subprocess.Popen, timeout_seconds: int = 5) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)
