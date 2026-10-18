from pathlib import Path

from local_app.config import get_config


def get_root_dir() -> Path:
    return get_config().root_dir


def get_logs_dir() -> Path:
    return get_config().logs_dir


def get_data_dir() -> Path:
    return get_config().data_dir


def get_config_dir() -> Path:
    return get_config().config_dir


def get_backups_dir() -> Path:
    return get_config().backups_dir


def get_chrome_profile_dir() -> Path:
    return get_config().chrome_profile_dir


def get_server_exe_path() -> Path:
    return get_config().server_exe_path


def ensure_runtime_dirs() -> None:
    config = get_config()
    for directory in (
        config.root_dir,
        config.logs_dir,
        config.data_dir,
        config.config_dir,
        config.backups_dir,
        config.chrome_profile_dir,
        config.chrome_logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
