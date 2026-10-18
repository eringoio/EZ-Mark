"""Prompt 10 gap coverage: roster group snapshot behavior.

Every new ``ExamStudent`` link must snapshot the student's current class on the
link itself (model session event ``_snapshot_roster_groups``), while an
explicitly chosen class on the link stays authoritative.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import ExamStudent, MockExam, Student


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        yield db


def _student(session: Session, number: str, name: str, class_name: str) -> Student:
    student = Student(student_number=number, name=name, class_name=class_name)
    session.add(student)
    session.commit()
    return student


def test_link_without_explicit_class_snapshots_student_group(session: Session) -> None:
    exam = MockExam(name="Snapshot Run", assessment_type="generic_test")
    session.add(exam)
    session.commit()
    student = _student(session, "S001", "Alice Anderson", "Class B")

    link = ExamStudent(exam_id=exam.id, student_id=student.id)
    session.add(link)
    session.commit()

    # The link carries its own copy of the group at import time.
    session.expire(link)
    assert link.class_name == "Class B"
    # Renaming the student's class later does not silently reopen history.
    student.class_name = "Class C"
    session.commit()
    session.refresh(link)
    assert link.class_name == "Class B"


def test_explicit_group_on_link_is_preserved(session: Session) -> None:
    exam = MockExam(name="Snapshot Run 2", assessment_type="generic_test")
    session.add(exam)
    session.commit()
    student = _student(session, "S002", "Bob Brown", "Class X")

    # The reimport flow keeps a staff-chosen group even when it differs from
    # the student's own class field.
    link = ExamStudent(exam_id=exam.id, student_id=student.id, class_name="Custom Group")
    session.add(link)
    session.commit()

    session.refresh(link)
    assert link.class_name == "Custom Group"
