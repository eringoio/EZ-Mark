"""Prompt 07 tests: local path resolution, demo isolation, synthetic seed, restrictions."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.database as app_database
from app.config import Settings, get_settings
from app.main import create_app
from app.models import MockExam
from app.services.demo_service import (
    DEMO_STUDENTS,
    reset_demo_data,
    reset_demo_on_start,
    seed_demo_data,
    wipe_demo_data,
)
from scripts import run_ezmark_server

UPLOAD_GUARD_DETAIL = "Uploads and imports are disabled in demo mode."
_ROSTER = {"file": ("roster.csv", b"student_number,name,class_name\n1,A,X")}
_SCAN = {"files": ("scan.png", b"\x89PNG fake")}
PACKAGE_TOKEN = {"token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}

DEMO_UPLOAD_ENDPOINTS = (
    ("/exams/1/students/import/preview", _ROSTER, {"token": "x"}),
    ("/exams/1/students/import/confirm", {}, {"import_payload": "[]"}),
    ("/exams/1/scan-batches/upload", _SCAN, {"token": "x"}),
    ("/exams/1/peer-review/scan-batches/upload", _SCAN, {"rater_group_id": "1"}),
    ("/sessions/import-package/preview", _ROSTER, {"token": "x"}),
    ("/sessions/import-package/confirm", _ROSTER, PACKAGE_TOKEN),
    ("/templates/import-package/preview", _ROSTER, {"token": "x"}),
    ("/templates/import-package/confirm", _ROSTER, PACKAGE_TOKEN),
)


@pytest.fixture()
def demo_env(monkeypatch):
    """Enable DEMO_MODE without touching the shared test data root/DB."""
    monkeypatch.setenv("DEMO_MODE", "1")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("DEMO_MODE", raising=False)
    get_settings.cache_clear()


def _clean_seed_env(monkeypatch, tmp_path) -> Path:
    """Remove DATABASE_URL/DATA_ROOT so the demo validator picks the demo root."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    demo_root = tmp_path / "demo-data"
    monkeypatch.setenv("DEMO_DATA_ROOT", str(demo_root))
    return demo_root


def _make_offline_db(tmp_path: Path):
    """Create an isolated, schema-v1-stamped sqlite database for seeding tests."""
    db_path = tmp_path / "demo.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False}
    )
    app_database.Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO schema_info (schema_version) VALUES (1)")
    return sessionmaker(bind=engine, autoflush=False, autocommit=False), engine


@pytest.fixture()
def initialized_db():
    """Run startup-style initialization on the shared test DB (TestClient without
    a context manager does not trigger lifespan events)."""
    app_database.init_database()


# --- Local path resolution (dev vs packaged) -------------------------------


def test_dev_runner_default_data_root_is_project_local(monkeypatch):
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert run_ezmark_server._default_data_root() == "data"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows packaged default")
def test_frozen_runner_default_data_root_is_per_user(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    assert run_ezmark_server._default_data_root() == str(
        tmp_path / "LocalAppData" / "EZMark" / "data"
    )


# --- Demo data-root isolation ---------------------------------------------


def test_demo_mode_defaults_to_separate_data_root(monkeypatch, tmp_path):
    demo_root = _clean_seed_env(monkeypatch, tmp_path)
    settings = Settings(demo_mode=True)
    assert settings.demo_mode is True
    assert settings.data_root == demo_root
    assert settings.database_url == f"sqlite:///{(demo_root / 'app.sqlite').as_posix()}"


def test_explicit_env_overrides_win_in_demo_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "portable-data"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'portable.sqlite').as_posix()}")
    settings = Settings()
    assert settings.data_root == tmp_path / "portable-data"
    assert settings.database_url == f"sqlite:///{(tmp_path / 'portable.sqlite').as_posix()}"


# --- Demo seed: idempotence, determinism, synthetic-only --------------------


def _seed_counts(db):
    from sqlalchemy import func, select

    from app.models import ExamStudent, MockExam, Question

    return {
        "templates": db.scalar(
            select(func.count()).select_from(MockExam).where(MockExam.is_template.is_(True))
        ),
        "runs": db.scalar(
            select(func.count()).select_from(MockExam).where(MockExam.is_template.is_(False))
        ),
        "questions": db.scalar(select(func.count()).select_from(Question)),
        "exam_students": db.scalar(select(func.count()).select_from(ExamStudent)),
    }


def test_seed_demo_data_is_idempotent(monkeypatch, tmp_path):
    _clean_seed_env(monkeypatch, tmp_path)
    SessionLocal, engine = _make_offline_db(tmp_path)
    monkeypatch.setattr(app_database, "SessionLocal", SessionLocal)

    with SessionLocal() as db:
        assert seed_demo_data(db) is True
    first = _seed_counts(SessionLocal())
    with SessionLocal() as db:
        assert seed_demo_data(db) is False  # second run: no dupes, no changes
    assert _seed_counts(SessionLocal()) == first


def test_seed_reset_is_deterministic_synthetic(monkeypatch, tmp_path):
    _clean_seed_env(monkeypatch, tmp_path)
    SessionLocal, engine = _make_offline_db(tmp_path)
    monkeypatch.setattr(app_database, "SessionLocal", SessionLocal)
    monkeypatch.setattr(app_database, "engine", engine)
    monkeypatch.setenv("DEMO_MODE", "1")
    get_settings.cache_clear()
    try:
        assert reset_demo_data() is True
        first = _seed_counts(SessionLocal())
        assert reset_demo_data() is True  # --reset wipes and re-creates
        assert _seed_counts(SessionLocal()) == first
    finally:
        monkeypatch.delenv("DEMO_MODE", raising=False)
        get_settings.cache_clear()

    from sqlalchemy import func, select

    from app.models import ExamStudent, MockExam, Student

    # Quiz questions: 20 on the template + 20 copied into the run.
    assert first == {"templates": 2, "runs": 2, "questions": 40, "exam_students": 24}

    db = SessionLocal()
    students = list(db.scalars(select(Student).order_by(Student.student_number)))
    assert [s.student_number for s in students] == sorted(number for number, _, _ in DEMO_STUDENTS)
    quiz_run = db.scalar(select(MockExam).where(MockExam.name == "Demo Quiz Run (Demo Group A)"))
    assert quiz_run is not None
    quiz_link_count = db.scalar(
        select(func.count()).select_from(ExamStudent).where(ExamStudent.exam_id == quiz_run.id)
    )
    assert quiz_link_count == 12
    db.close()


def test_wipe_and_reset_obey_demo_flag(monkeypatch):
    # DEMO_MODE off: wipe refuses, reset-on-start does nothing.
    monkeypatch.delenv("DEMO_MODE", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            wipe_demo_data()
        assert reset_demo_on_start() is None
    finally:
        get_settings.cache_clear()


# --- Demo restrictions vs normal mode --------------------------------------


def test_demo_mode_blocks_uploads_and_imports(demo_env, initialized_db):
    client = TestClient(create_app())
    for endpoint, files, data in DEMO_UPLOAD_ENDPOINTS:
        response = client.post(endpoint, files=files, data=data)
        assert response.status_code == 403, (endpoint, response.status_code)
        assert UPLOAD_GUARD_DETAIL in response.text


def test_normal_mode_keeps_upload_paths_reachable(initialized_db):
    client = TestClient(create_app())
    for endpoint, files, data in DEMO_UPLOAD_ENDPOINTS:
        response = client.post(endpoint, files=files, data=data)
        assert response.status_code != 403, endpoint


def test_demo_indicator_shown_in_demo_mode_and_hidden_locally(
    demo_env, initialized_db, monkeypatch
):
    client = TestClient(create_app())
    response = client.get("/")
    assert response.status_code == 200
    assert "Demo Mode" in response.text
    assert "synthetic example data" in response.text

    # A normal local run (DEMO_MODE off) must not show the demo indicator.
    monkeypatch.delenv("DEMO_MODE", raising=False)
    get_settings.cache_clear()
    normal = TestClient(create_app())
    normal_page = normal.get("/")
    assert normal_page.status_code == 200
    assert "Demo Mode" not in normal_page.text


def test_demo_locale_keys_have_en_ja_parity():
    repo = Path(__file__).resolve().parents[1]
    en = json.loads((repo / "app" / "locales" / "en.json").read_text(encoding="utf-8"))
    ja = json.loads((repo / "app" / "locales" / "ja.json").read_text(encoding="utf-8"))
    assert set(en["demo"]) == set(ja["demo"])
    for key, en_value in en["demo"].items():
        assert en_value and ja["demo"][key]


def test_demo_quiz_template_is_a_usable_twenty_question_run(monkeypatch, tmp_path):
    """The seeded determinstic quiz is a real, scoreable 20-question run."""
    _clean_seed_env(monkeypatch, tmp_path)
    SessionLocal, engine = _make_offline_db(tmp_path)
    monkeypatch.setattr(app_database, "SessionLocal", SessionLocal)

    seed_demo_data(SessionLocal())

    from sqlalchemy import select

    from app.models import Question, ScoringSection

    db = SessionLocal()
    template = db.scalar(
        select(MockExam).where(MockExam.name == "Demo Quiz: Multiple Choice (20 Questions)")
    )
    assert template is not None
    quiz = db.scalar(select(MockExam).where(MockExam.name == "Demo Quiz Run (Demo Group A)"))
    assert quiz is not None

    questions = list(
        db.scalars(
            select(Question).where(Question.exam_id == quiz.id).order_by(Question.question_no)
        )
    )
    assert len(questions) == 20
    # Every question is identifiable, has 4 configured choices, and a valid key.
    for index, question in enumerate(questions, start=1):
        assert question.question_no == index
        assert question.choice_count == 4
        assert question.correct_answer in {"1", "2", "3", "4"}
        assert question.points is not None
        assert (
            db.scalars(
                select(ScoringSection.id).where(ScoringSection.id == question.scoring_section_id)
            ).first()
            is not None
        )

    # The run is a real, self-contained copy of the template quiz: same answer
    # key, roster attached, so it only needs scans to be scoreable.
    assert [question.correct_answer for question in questions[:4]] == ["1", "2", "3", "4"]
    db_template = db.scalar(
        select(MockExam).where(MockExam.name == "Demo Quiz: Multiple Choice (20 Questions)")
    )
    template_questions = list(
        db.scalars(
            select(Question)
            .where(Question.exam_id == db_template.id)
            .order_by(Question.question_no)
        )
    )
    assert [q.correct_answer for q in template_questions] == [q.correct_answer for q in questions]
    db.close()
