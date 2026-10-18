import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "EZ Mark"
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = "http://127.0.0.1:8000"
HEALTH_URL = "http://127.0.0.1:8000/healthz"


@dataclass(frozen=True)
class LauncherConfig:
    app_name: str
    host: str
    port: int
    root_dir: Path
    server_exe_path: Path
    chrome_exe_path: Path | None
    dev_server_cmd: str | None
    log_level: str

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/healthz"

    @property
    def data_dir(self) -> Path:
        return _persistent_base(self.root_dir) / "data"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.sqlite"

    @property
    def database_url(self) -> str:
        return "sqlite:///" + self.database_path.as_posix()

    @property
    def logs_dir(self) -> Path:
        return _persistent_base(self.root_dir) / "logs"

    @property
    def config_dir(self) -> Path:
        return _persistent_base(self.root_dir) / "config"

    @property
    def local_env_path(self) -> Path:
        return self.config_dir / "local.env"

    @property
    def backups_dir(self) -> Path:
        return _persistent_base(self.root_dir) / "backups"

    @property
    def chrome_profile_dir(self) -> Path:
        return get_user_local_dir() / "chrome-profile"

    @property
    def chrome_logs_dir(self) -> Path:
        return get_user_local_dir() / "logs"

    @property
    def chrome_launch_log_path(self) -> Path:
        return self.chrome_logs_dir / "chrome-launch.log"

    @property
    def server_dir(self) -> Path:
        return self.root_dir / "server"


def _read_port() -> int:
    value = os.getenv("EZMARK_PORT", str(PORT))
    try:
        return int(value)
    except ValueError:
        return PORT


def get_default_root_dir() -> Path:
    # Application/binaries root. Persisted data may live elsewhere (see
    # ``persistent_base``): a packaged install under e.g. Program Files is not
    # writable, so per-user data lives under %LOCALAPPDATA%\EZMark. Set
    # ``EZMARK_ROOT`` for a deliberately portable install where data, logs and
    # configuration stay beside the executable.
    configured_root = os.getenv("EZMARK_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parents[1]


def _persistent_base(root_dir: Path) -> Path:
    """Base directory for data/logs/config/backups.

    * packaged (frozen) installs default to the per-user ``%LOCALAPPDATA%\\EZMark``
      (``get_user_local_dir()``) so the install directory does not need to be
      writable;
    * ``EZMARK_ROOT`` keeps everything portable beside the executable;
    * development keeps everything in the repository.
    """
    if os.getenv("EZMARK_ROOT"):
        return root_dir
    if getattr(sys, "frozen", False):
        return get_user_local_dir()
    return root_dir


def get_user_local_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "EZMark"
        return Path.home() / "AppData" / "Local" / "EZMark"

    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "ezmark"
    return Path.home() / ".local" / "share" / "ezmark"


def get_config() -> LauncherConfig:
    root_dir = get_default_root_dir()
    server_exe_override = os.getenv("EZMARK_SERVER_EXE")
    server_exe = Path(server_exe_override) if server_exe_override else root_dir / "server" / "ezmark-server.exe"
    chrome_exe = os.getenv("EZMARK_CHROME_EXE")
    return LauncherConfig(
        app_name=APP_NAME,
        host=os.getenv("EZMARK_HOST", HOST),
        port=_read_port(),
        root_dir=root_dir,
        server_exe_path=server_exe,
        chrome_exe_path=Path(chrome_exe) if chrome_exe else None,
        dev_server_cmd=os.getenv("EZMARK_DEV_SERVER_CMD"),
        log_level=os.getenv("EZMARK_LOG_LEVEL", "info"),
    )


def parse_local_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values
