import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    Student,
    StudentResult,
)
from app.services.exam_service import (
    ExamCreateInput,
    ExamUpdateInput,
    create_exam,
    delete_exam,
    update_exam,
)


def test_generic_exam_creation_started_in_draft() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam = create_exam(
            session,
            ExamCreateInput(
                name="Synthetic Generic Test",
                assessment_type="generic_test",
                exam_date="2026-07-01",
                scoring_mode="generic_raw",
                numbering_mode="continuous",
            ),
        )

        assert exam.status == "draft"
        assert exam.scoring_mode == "generic_raw"


def test_create_exam_rejects_unknown_assessment_type() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        with pytest.raises(ValueError):
            create_exam(
                session,
                ExamCreateInput(
                    name="Bad",
                    assessment_type="unsupported_type",
                    exam_date=None,
                    scoring_mode="generic_raw",
                    numbering_mode="continuous",
                ),
            )


def test_update_exam_changes_name_date_and_level() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = create_exam(
            session,
            ExamCreateInput(
                name="Original",
                assessment_type="generic_test",
                exam_date=None,
                scoring_mode="generic_raw",
                numbering_mode="continuous",
            ),
        )

        update_exam(
            session,
            exam,
            ExamUpdateInput(name="Renamed", exam_date="2026-07-01"),
        )

        assert exam.name == "Renamed"
        assert exam.exam_date == "2026-07-01"


def test_update_exam_rejects_bad_date_and_empty_name() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = create_exam(
            session,
            ExamCreateInput(
                name="Original",
                assessment_type="generic_test",
                        exam_date=None,
                scoring_mode="generic_raw",
                numbering_mode="continuous",
            ),
        )

        with pytest.raises(ValueError):
            update_exam(session, exam, ExamUpdateInput(name="x", exam_date="07/01/2026"))
        with pytest.raises(ValueError):
            update_exam(session, exam, ExamUpdateInput(name="   "))


def test_delete_exam_removes_record() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = create_exam(
            session,
            ExamCreateInput(
                name="To delete",
                assessment_type="generic_test",
                exam_date=None,
                scoring_mode="generic_raw",
                numbering_mode="continuous",
            ),
        )
        exam_id = exam.id

        delete_exam(session, exam)

        assert session.get(MockExam, exam_id) is None


def test_delete_exam_keeps_student_linked_to_another_session() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        first_exam = MockExam(name="Run 1", assessment_type="generic_test")
        second_exam = MockExam(name="Run 2", assessment_type="generic_test")
        student = Student(student_number="S200", name="Shared Student", class_name="A")
        session.add_all([first_exam, second_exam, student])
        session.commit()
        session.add_all(
            [
                ExamStudent(exam_id=first_exam.id, student_id=student.id, included=True),
                ExamStudent(exam_id=second_exam.id, student_id=student.id, included=True),
            ]
        )
        session.commit()
        first_exam_id = first_exam.id
        second_exam_id = second_exam.id
        student_id = student.id

        delete_exam(session, first_exam)

        assert session.get(MockExam, first_exam_id) is None
        assert session.get(Student, student_id) is not None

        second_exam = session.get(MockExam, second_exam_id)
        assert second_exam is not None
        delete_exam(session, second_exam)

        assert session.get(MockExam, second_exam_id) is None
        assert session.get(Student, student_id) is None


def test_delete_exam_removes_unique_student_and_session_owned_results() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Unique Run", assessment_type="generic_test")
        student = Student(student_number="S201", name="Unique Student", class_name="A")
        session.add_all([exam, student])
        session.commit()
        session.add_all(
            [
                ExamStudent(exam_id=exam.id, student_id=student.id, included=True),
                StudentResult(
                    exam_id=exam.id,
                    student_id=student.id,
                    raw_json={"total": 10},
                    score_details_json={"total": 10},
                    total_score=10,
                ),
            ]
        )
        session.commit()
        exam_id = exam.id
        student_id = student.id

        delete_exam(session, exam)

        assert session.get(MockExam, exam_id) is None
        assert session.query(ExamStudent).filter_by(student_id=student_id).count() == 0
        assert session.query(StudentResult).filter_by(student_id=student_id).count() == 0
        assert session.get(Student, student_id) is None
        assert session.query(Student).filter_by(student_number="S201").count() == 0


def test_delete_peer_review_exam_cascades_peer_review_records_and_orphan_students() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Peer Run", assessment_type="peer_review")
        target = Student(student_number="S302", name="Target", class_name="A")
        session.add_all([exam, target])
        session.commit()
        competency = Competency(exam_id=exam.id, name="Clarity", display_order=1)
        session.add(competency)
        session.commit()
        session.add(ExamStudent(exam_id=exam.id, student_id=target.id, included=True))
        session.commit()
        exam_id = exam.id
        target_id = target.id

        delete_exam(session, exam)

        assert session.get(MockExam, exam_id) is None
        assert session.get(Student, target_id) is None


def test_roster_removal_does_not_delete_student() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Roster Run", assessment_type="generic_test")
        student = Student(student_number="S401", name="Roster Student", class_name="A")
        session.add_all([exam, student])
        session.commit()
        link = ExamStudent(exam_id=exam.id, student_id=student.id, included=True)
        session.add(link)
        session.commit()

        link.included = False
        session.commit()

        assert session.get(Student, student.id) is not None
        assert session.get(ExamStudent, link.id).included is False
