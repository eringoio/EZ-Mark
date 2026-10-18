from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import MockExam
from app.services.student_import_service import (
    import_preview_to_payload,
    import_students_from_payload,
    parse_student_import,
)


def test_csv_student_import_preview_flags_duplicate_rows() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    content = b"Student Number,Name,Group\nSYN001,Student A,A\nSYN001,Student A Again,A\n"
    with Session(engine) as session:
        preview = parse_student_import("students.csv", content, session)

    assert len(preview.valid_rows) == 1
    assert preview.error_rows[0].message == "Duplicate in file"


def test_confirm_import_creates_students_and_exam_links() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    content = b"Student Number,Name,Group\nSYN001,Student A,A\n"
    with Session(engine) as session:
        exam = MockExam(name="Synthetic Exam")
        session.add(exam)
        session.commit()

        preview = parse_student_import("students.csv", content, session)
        count = import_students_from_payload(session, exam, import_preview_to_payload(preview))

        assert count == 1
        assert len(exam.students) == 1
        assert exam.students[0].student.student_number == "SYN001"
