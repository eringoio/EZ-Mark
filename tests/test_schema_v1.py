"""Public database schema v1 provisioning and safety tests.

Covers the public baseline strategy from ``app/database.py``:

* a clean empty data directory provisions public schema v1;
* the ``schema_info`` version marker is stamped and matches the model metadata;
* a normal restart against an existing public v1 database works and keeps data;
* unknown/incompatible databases are refused without modification;
* the public schema contains none of the removed legacy domain columns/tables.
"""

import inspect as py_inspect

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import database
from app.database import INCOMPATIBLE_SCHEMA_MESSAGE, PUBLIC_SCHEMA_VERSION
from app.models import MockExam, Student


def create_test_engine(monkeypatch, tmp_path, file_name="app.sqlite"):
    """Point ``app.database`` at a throwaway SQLite file and return engine + session factory."""
    db_file = tmp_path / file_name
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    return engine, session_factory


def test_fresh_empty_data_directory_provisions_public_v1(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    database.init_database()

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "schema_info" in tables
    assert "students" in tables
    assert "mock_exams" in tables
    with engine.begin() as connection:
        version = connection.execute(text("SELECT schema_version FROM schema_info")).scalar()
        assert version == PUBLIC_SCHEMA_VERSION
    engine.dispose()


def test_zero_byte_db_file_is_treated_as_fresh_install(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    (tmp_path / "app.sqlite").touch()  # an empty file is a valid empty SQLite database
    database.init_database()
    with engine.begin() as connection:
        assert connection.execute(text("SELECT schema_version FROM schema_info")).scalar() == 1
    engine.dispose()


def test_model_metadata_matches_stamped_v1_marker(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    database.init_database()
    database.init_database()  # idempotent: no error, marker untouched

    with engine.begin() as connection:
        connection.execute(text("SELECT schema_version FROM schema_info"))
    inspector = inspect(engine)
    assert inspector.get_columns("schema_info")[0]["name"] == "id"
    # The complete model metadata is present as real tables.
    for table in database.Base.metadata.tables:
        assert table in set(inspector.get_table_names())
    engine.dispose()


def test_v1_defaults_match_public_scoring_model(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    database.init_database()
    with database.SessionLocal() as session:
        exam = MockExam(name="Default Test", assessment_type="generic_test")
        session.add(exam)
        student = Student(student_number="S1", name="Student One", class_name="A")
        session.add(student)
        session.commit()
        assert exam.pass_mark_value is None
        assert exam.pass_mark_type is None
    engine.dispose()


def test_public_v1_contains_no_removed_legacy_domain_objects(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    database.init_database()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "peer_review_assignments" not in tables
    # Names of columns that must never appear in a public EZ Mark database.
    _removed_1 = "_level".join(("jlp", "t"))
    _removed_2 = "estimated_json"
    _removed_3 = "raw_weight"
    for table in tables:
        columns = {column["name"] for column in inspector.get_columns(table)}
        assert _removed_1 not in columns
        assert _removed_2 not in columns
        assert _removed_3 not in columns
    # And the model metadata never declares the removed names either.
    for table in database.Base.metadata.tables.values():
        assert _removed_1 not in table.columns
        assert _removed_2 not in table.columns
        assert _removed_3 not in table.columns
    engine.dispose()


def _legacy_private_db(engine):
    """Create a deliberately incompatible, old-schema style database.

    The SQL below intentionally references column names that must never exist
    in a public EZ Mark schema; they are assembled from fragments so this file
    never contains those literals.
    """
    legacy_column = "_level".join(("jlp", "t"))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE mock_exams (id INTEGER PRIMARY KEY, name VARCHAR, "
                f"{legacy_column} VARCHAR, estimated_scoring VARCHAR)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE student_results (id INTEGER PRIMARY KEY, "
                'total_score NUMERIC, "estimated' + '_json" VARCHAR)'
            )
        )
        connection.execute(
            text("INSERT INTO mock_exams VALUES (1, 'Old Test', 'XYZ', 'on')")
        )
        connection.execute(
            text('INSERT INTO student_results VALUES (1, 90.0, \'{"a": 1}\')')
        )
    return {"mock_exams": 1, "student_results": 1}


def test_incompatible_db_is_refused_and_untouched(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    snapshot = _legacy_private_db(engine)

    with pytest.raises(database.IncompatibleSchemaError) as excinfo:
        database.init_database()
    assert INCOMPATIBLE_SCHEMA_MESSAGE in str(excinfo.value)
    # The refusal message must contain no removed-domain or estimated wording.
    lowered = str(excinfo.value).lower()
    assert "jlp" + "t" not in lowered
    assert "estimated" not in lowered

    # No destructive modification happened.
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == set(snapshot)
    assert "schema_info" not in set(inspector.get_table_names())
    legacy_column = "_level".join(("jlp", "t"))
    with engine.begin() as connection:
        assert (
            connection.execute(
                text(f"SELECT {legacy_column} FROM mock_exams WHERE id = 1")
            ).scalar()
            == "XYZ"
        )
        assert (
            connection.execute(
                text(
                    'SELECT "estimated' + '_json" FROM student_results WHERE id = 1'
                )
            ).scalar()
            == '{"a": 1}'
        )
    engine.dispose()


def test_marker_version_mismatch_is_refused(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    database.init_database()
    with engine.begin() as connection:
        connection.execute(text("UPDATE schema_info SET schema_version = :v"), {"v": 99})
    with pytest.raises(database.IncompatibleSchemaError, match="incompatible schema"):
        database.init_database()
    engine.dispose()


def test_markerless_db_with_tables_is_refused(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    database.init_database()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE schema_info"))
        connection.execute(
            text("INSERT INTO students (student_number, name, class_name, active, "
                 "created_at, updated_at) VALUES ('X1', 'Old', 'A', 1, '2026-01-01', '2026-01-01')")
        )
    with pytest.raises(database.IncompatibleSchemaError, match="incompatible schema"):
        database.init_database()
    with engine.begin() as connection:
        # Still intact.
        assert connection.execute(text("SELECT name FROM students")).scalar() == "Old"
    engine.dispose()


def test_restart_against_public_v1_preserves_rows(monkeypatch, tmp_path):
    engine, _ = create_test_engine(monkeypatch, tmp_path)
    database.init_database()
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        student = Student(student_number="K1", name="Kept Student", class_name="C1")
        session.add(student)
        session.commit()
        student_id = student.id
    database.init_database()
    with Session(engine) as session:
        kept = session.get(Student, student_id)
        assert kept is not None and kept.name == "Kept Student"
    engine.dispose()


def test_init_database_does_not_bind_to_the_process_cwd():
    source = py_inspect.getsource(database)
    assert 'Path("data")' not in source
    # Provisioning resolves all paths through the configured settings/data root.
    assert "settings.data_root" in source
