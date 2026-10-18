from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


DATA_SUBDIRECTORIES = (
    "generated_pdfs",
    "uploaded_scans",
    "processed_pages",
    "review_crops",
    "exports",
    "import_previews",
    "package_exports",
)


def ensure_data_directories() -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    for directory in DATA_SUBDIRECTORIES:
        (settings.data_root / directory).mkdir(parents=True, exist_ok=True)


# Public database schema strategy
# --------------------------------
# EZ Mark uses a single versioned public baseline: ``Base.metadata.create_all()``
# provisions a fresh database from the current SQLAlchemy models, and the
# ``schema_info`` table marks the database as EZ Mark public schema v1.
#
# On startup an existing database file is validated against this marker:
#
# * an empty database (or a zero-byte file, which is a valid empty database) is
#   provisioned as public schema v1;
# * a database marked as public v1 with the matching version is opened as-is;
# * anything else (a legacy private-app database, a database missing the
#   marker, or an unknown future version) is refused without modification.
#
# Alembic is installed, but the packaged app (PyInstaller) plus a tagged
# baseline is deliberately simpler and works without shipping migration files,
# so ``create_all`` + the version marker is the canonical mechanism. When the
# public schema itself evolves, a new schema version will be introduced with
# its own explicit provisioning path.

PUBLIC_SCHEMA_VERSION = 1

INCOMPATIBLE_SCHEMA_MESSAGE = (
    "This database uses an incompatible schema. Back it up and start EZ Mark "
    "with a fresh data directory, or use a compatible EZ Mark version to "
    "migrate it."
)


class IncompatibleSchemaError(RuntimeError):
    """Raised when the existing database is not a valid EZ Mark public schema."""


def _read_schema_version(tables: set[str]) -> int | None:
    """Return the stored schema version, or ``None`` when the marker is absent/invalid."""
    if "schema_info" not in tables:
        return None
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT schema_version FROM schema_info")).fetchall()
    except Exception:  # noqa: BLE001 - unreadable marker means incompatible database
        return None
    if len(rows) != 1:
        return None
    version = rows[0][0]
    return int(version) if version is not None else None


def init_database() -> None:
    ensure_data_directories()
    import app.models  # noqa: F401

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if tables:
        version = _read_schema_version(tables)
        if version is None or version != PUBLIC_SCHEMA_VERSION:
            # Unknown database: never modify it in place.
            raise IncompatibleSchemaError(INCOMPATIBLE_SCHEMA_MESSAGE)
        return

    # A database with zero tables is a fresh install: provision the public v1
    # schema and stamp the version marker.
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO schema_info (schema_version) VALUES (:v)"),
            {"v": PUBLIC_SCHEMA_VERSION},
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def data_relative_path(path: Path) -> str:
    return str(path.relative_to(settings.data_root)).replace("\\", "/")


if __name__ == "__main__":
    init_database()
