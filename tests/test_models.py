from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import MockExam, PhysicalSheet, ScoringSection, Student


def test_core_models_can_be_persisted() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam = MockExam(
            name="Synthetic Sample Test",
            assessment_type="generic_test",
            exam_date="2026-07-01",
            scoring_mode="generic_raw",
            numbering_mode="continuous",
        )
        student = Student(student_number="SYN001", name="Student A", class_name="A")
        sheet = PhysicalSheet(sheet_no=1, sheet_name="文字・語彙", slug="language", display_order=1)
        section = ScoringSection(
            code="language_knowledge",
            name="Language Knowledge",
            display_order=1,
        )
        exam.physical_sheets.append(sheet)
        exam.scoring_sections.append(section)
        session.add_all([exam, student])
        session.commit()

        assert exam.id is not None
        assert student.id is not None
        assert exam.physical_sheets[0].sheet_name == "文字・語彙"
        assert exam.scoring_sections[0].name == "Language Knowledge"
