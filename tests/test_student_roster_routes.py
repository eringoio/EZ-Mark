from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import ExamStudent, MockExam, Student
from app.routes.students import _apply_student_fields, _validate_student_update


def test_manual_roster_edit_validation_and_remove_flag() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Roster Run", assessment_type="generic_test")
        student = Student(student_number="S100", name="Student One", class_name="A")
        duplicate = Student(student_number="S101", name="Student Two", class_name="A")
        session.add_all([exam, student, duplicate])
        session.commit()
        link = ExamStudent(
            exam_id=exam.id,
            student_id=student.id,
            seat_order=2,
            included=True,
        )
        session.add(link)
        session.commit()

        errors = _validate_student_update(session, student, "S101", "Student One", "A")
        assert "Student number already exists." in errors

        errors = _validate_student_update(session, student, "S100", "Student Renamed", "B")
        assert errors == []
        _apply_student_fields(student, "S100", "Student Renamed", "B", True)
        link.seat_order = 1
        link.included = False
        session.commit()

        assert student.name == "Student Renamed"
        assert student.class_name == "B"
        assert link.seat_order == 1
        assert link.included is False
