"""Regression: the normal marksheet always prints the real linked Student.name.

Guards against the reported "first student is always 'test user'" symptom by asserting
the printed PDF carries the actual roster names (and never a stray placeholder), and that
``SheetInstance.printed_name`` is refreshed when a student is renamed and sheets regenerated.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base
from app.models import ExamStudent, MockExam, PhysicalSheet, Question, ScoringSection, Student
from app.services.sheet_generation_service import generate_exam_sheets
from tests.pdf_helpers import full_text


def _exam_with_three_students(session: Session) -> tuple[MockExam, list[Student]]:
    exam = MockExam(
        name="Names Run",
        assessment_type="generic_test",
        exam_date="2026-06-10",
    )
    sheet = PhysicalSheet(sheet_no=1, sheet_name="文字・語彙", slug="vocab", display_order=1)
    section = ScoringSection(code="lk", name="Language")
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    students = [
        Student(student_number="S001", name="Alice Anderson", class_name="A"),
        Student(student_number="S002", name="Bob Brown", class_name="A"),
        Student(student_number="S003", name="Carol Clark", class_name="A"),
    ]
    session.add(exam)
    session.add_all(students)
    session.commit()
    for order, student in enumerate(students, start=1):
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id, seat_order=order))
    for index in range(1, 6):
        session.add(
            Question(
                exam_id=exam.id,
                physical_sheet_id=sheet.id,
                scoring_section_id=section.id,
                question_no=index,
                display_label=str(index),
                correct_answer="1",
                choice_count=4,
                display_order=index,
            )
        )
    session.commit()
    return exam, students


def _pdf_text(relative_path: str) -> str:
    return full_text(get_settings().data_root / relative_path)


def test_normal_marksheet_prints_real_names_not_placeholder() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, students = _exam_with_three_students(session)
        summary = generate_exam_sheets(session, exam)
        text = _pdf_text(summary.download_path)

        for name in ("Alice Anderson", "Bob Brown", "Carol Clark"):
            assert name in text
        assert "test user" not in text.lower()
        assert "Print at 100% scale" not in text
        # Every printed instance carries its linked student's real name.
        for instance in exam.sheet_instances:
            student = session.get(Student, instance.student_id)
            assert instance.printed_name == student.name


def test_edited_marksheet_title_is_printed_without_bottom_instruction() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, _students = _exam_with_three_students(session)
        exam.physical_sheets[0].sheet_name = "編集済みタイトル"
        session.commit()

        summary = generate_exam_sheets(session, exam)
        text = _pdf_text(summary.download_path)

        assert "編集済みタイトル" in text
        assert "印刷スケール" not in text


def test_printed_name_updates_on_regeneration_after_rename() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam, students = _exam_with_three_students(session)
        generate_exam_sheets(session, exam)

        students[0].name = "Renamed Student"
        session.commit()
        summary = generate_exam_sheets(session, exam)

        text = _pdf_text(summary.download_path)
        assert "Renamed Student" in text
        assert "Alice Anderson" not in text
        instance = next(i for i in exam.sheet_instances if i.student_id == students[0].id)
        assert instance.printed_name == "Renamed Student"
