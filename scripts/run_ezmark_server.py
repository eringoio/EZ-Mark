import logging
import os
import sys
from pathlib import Path

import uvicorn


def _ensure_repo_root_on_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    if getattr(sys, "frozen", False):
        return repo_root

    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return repo_root


def _safe_database_url(database_url: str) -> str:
    if "@" not in database_url:
        return database_url
    scheme, _, rest = database_url.partition("://")
    _, _, host_part = rest.rpartition("@")
    return f"{scheme}://***@{host_part}"


def _default_data_root() -> str:
    """Default persistent data root.

    Development keeps the project-local ``data/`` directory. A packaged
    (frozen) server does not assume its install directory is writable: it
    defaults to the per-user EZ Mark data location (``%LOCALAPPDATA%\\EZMark``
    on Windows / XDG paths elsewhere). ``DATA_ROOT`` overrides both.
    """
    if getattr(sys, "frozen", False):
        try:
            from local_app.config import get_user_local_dir

            return str(Path(get_user_local_dir()) / "data")
        except ImportError:
            # Fallback for bare frozen bundles without the launcher package.
            if sys.platform == "win32":
                base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
                return str(Path(base) / "EZMark" / "data")
            xdg = os.getenv("XDG_DATA_HOME")
            base = Path(xdg) if xdg else Path.home() / ".local" / "share"
            return str(base / "ezmark" / "data")
    return "data"


def main() -> None:
    _ensure_repo_root_on_path()

    # Apply packaged defaults before the app settings are imported. Explicit
    # env (DATA_ROOT / DATABASE_URL / DEMO_*) always wins.
    data_root = os.getenv("DATA_ROOT") or _default_data_root()
    os.environ.setdefault("DATA_ROOT", data_root)
    if getattr(sys, "frozen", False) and not os.getenv("DATABASE_URL"):
        database_url = "sqlite:///" + (Path(data_root) / "app.sqlite").as_posix()
        os.environ["DATABASE_URL"] = database_url

    from app.main import app as fastapi_app

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    log_level = os.getenv("LOG_LEVEL", "info")
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/app.sqlite")

    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO))
    logging.info(
        "EZ Mark server runtime: ENV=%s HOST=%s PORT=%s DATABASE_URL=%s DATA_ROOT=%s "
        "DEMO_MODE=%s DEMO_RESET_ON_START=%s LOG_PATH=%s",
        os.getenv("ENV", "development"),
        host,
        port,
        _safe_database_url(database_url),
        data_root,
        os.getenv("DEMO_MODE", "0"),
        os.getenv("DEMO_RESET_ON_START", "0"),
        os.getenv("LOG_PATH", "stdout"),
    )

    uvicorn.run(
        fastapi_app,
        host=host,
        port=port,
        log_level=log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
