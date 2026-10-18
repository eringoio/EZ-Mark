from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import MockExam, PhysicalSheet, ScoringSection
from app.services.answer_key_service import (
    apply_problem_group_counts_to_sheet,
    generate_continuous_questions,
    generate_grouped_questions,
    save_answer_key,
)


def _exam_with_sheet_and_section(session: Session) -> MockExam:
    exam = MockExam(name="Synthetic Exam", assessment_type="generic_test")
    exam.physical_sheets.append(
        PhysicalSheet(sheet_no=1, sheet_name="文字・語彙", slug="vocabulary", display_order=1)
    )
    exam.scoring_sections.append(
        ScoringSection(
            code="language_knowledge",
            name="Language Knowledge",
            display_order=1,
        )
    )
    session.add(exam)
    session.commit()
    return exam


def test_generate_continuous_questions_creates_display_labels() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = _exam_with_sheet_and_section(session)
        count = generate_continuous_questions(
            session,
            exam,
            total_questions=3,
            choice_count=4,
            physical_sheet_id=exam.physical_sheets[0].id,
            scoring_section_id=exam.scoring_sections[0].id,
        )

        assert count == 3
        assert [question.display_label for question in exam.questions] == ["1", "2", "3"]


def test_generate_grouped_questions_creates_problem_labels() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = _exam_with_sheet_and_section(session)
        count = generate_grouped_questions(
            session,
            exam,
            group_counts=[2, 1],
            choice_count=4,
            physical_sheet_id=exam.physical_sheets[0].id,
            scoring_section_id=exam.scoring_sections[0].id,
        )

        assert count == 3
        assert [question.display_label for question in exam.questions] == [
            "問題1-問1",
            "問題1-問2",
            "問題2-問1",
        ]


def test_apply_problem_group_counts_preserves_existing_answers() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = _exam_with_sheet_and_section(session)
        generate_continuous_questions(
            session,
            exam,
            total_questions=6,
            choice_count=4,
            physical_sheet_id=exam.physical_sheets[0].id,
            scoring_section_id=exam.scoring_sections[0].id,
        )
        for question in exam.questions:
            question.correct_answer = "2"
        session.commit()

        count = apply_problem_group_counts_to_sheet(
            session,
            exam,
            physical_sheet_id=exam.physical_sheets[0].id,
            group_counts=[2, 4],
        )

        assert count == 6
        assert [group.subquestion_count for group in exam.problem_groups] == [2, 4]
        assert [question.problem_no for question in exam.questions] == [1, 1, 2, 2, 2, 2]
        assert [question.local_question_no for question in exam.questions] == [1, 2, 1, 2, 3, 4]
        assert [question.display_label for question in exam.questions] == [
            "問題1-問1",
            "問題1-問2",
            "問題2-問1",
            "問題2-問2",
            "問題2-問3",
            "問題2-問4",
        ]
        assert {question.correct_answer for question in exam.questions} == {"2"}


def test_save_answer_key_validates_choice_range() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = _exam_with_sheet_and_section(session)
        generate_continuous_questions(
            session,
            exam,
            total_questions=1,
            choice_count=4,
            physical_sheet_id=exam.physical_sheets[0].id,
            scoring_section_id=exam.scoring_sections[0].id,
        )
        issues = save_answer_key(session, exam, {exam.questions[0].id: "5"})

        assert issues[0].message == "Correct answer must be within the choice range."
