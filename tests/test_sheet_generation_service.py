from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base
from app.models import ExamStudent, MockExam, PhysicalSheet, Question, ScoringSection, Student
from app.services.sheet_generation_service import (
    HAS_JAPANESE_FONT,
    JAPANESE_FONT_IS_EMBEDDED,
    LANDSCAPE_A4_HEIGHT_MM,
    LANDSCAPE_A4_WIDTH_MM,
    _coordinate_map_for_questions,
    _get_fonts,
    calculate_sheet_layout,
    generate_exam_sheets,
)
from tests.pdf_helpers import extract_text, open_document


def test_coordinate_map_uses_question_ids_and_millimeters() -> None:
    question = Question(id=12, display_label="1", choice_count=4)

    coordinate_map = _coordinate_map_for_questions([question])

    assert coordinate_map["questions"][0]["question_id"] == 12
    assert coordinate_map["page_width_mm"] == LANDSCAPE_A4_WIDTH_MM
    assert coordinate_map["page_height_mm"] == LANDSCAPE_A4_HEIGHT_MM
    assert coordinate_map["questions"][0]["choices"][0]["x_mm"] > 0
    assert coordinate_map["questions"][0]["choices"][0]["radius_mm"] == 2.2


def test_pdf_font_for_japanese_text_is_not_helvetica_when_cid_font_available() -> None:
    font_regular, font_bold = _get_fonts()

    assert HAS_JAPANESE_FONT is True
    assert font_regular != "Helvetica"
    assert font_bold != "Helvetica-Bold"
    if Path("/mnt/c/Windows/Fonts/NotoSansJP-VF.ttf").exists():
        assert JAPANESE_FONT_IS_EMBEDDED is True


def test_sheet_generation_prerequisites_are_model_compatible() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Synthetic Exam", assessment_type="generic_test")
        student = Student(student_number="SYN001", name="Student A", class_name="A")
        sheet = PhysicalSheet(
            sheet_no=1, sheet_name="文字・語彙", slug="vocabulary", display_order=1
        )
        section = ScoringSection(
            code="language_knowledge",
            name="Language Knowledge",
        )
        exam.physical_sheets.append(sheet)
        exam.scoring_sections.append(section)
        session.add_all([exam, student])
        session.commit()
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
        session.add(
            Question(
                exam_id=exam.id,
                physical_sheet_id=sheet.id,
                scoring_section_id=section.id,
                question_no=1,
                display_label="1",
                correct_answer="1",
                choice_count=4,
            )
        )
        session.commit()

        assert len(exam.students) == 1
        assert len(exam.questions) == 1


def test_70_question_layout_is_landscape_grouped_and_in_bounds() -> None:
    questions = [
        Question(
            id=index,
            question_no=index,
            problem_no=((index - 1) // 10) + 1,
            local_question_no=((index - 1) % 10) + 1,
            display_label=str(index),
            choice_count=4,
            display_order=index,
        )
        for index in range(1, 71)
    ]

    layout = calculate_sheet_layout(questions)
    coordinate_map = _coordinate_map_for_questions(questions)

    assert layout.page_width_mm == 297
    assert len(layout.groups) >= 5
    assert len(coordinate_map["questions"]) == 70
    assert len({question["question_id"] for question in coordinate_map["questions"]}) == 70
    assert {group["group_label"] for group in coordinate_map["groups"]} >= {
        "もんだい1",
        "もんだい2",
    }
    for question in coordinate_map["questions"]:
        for choice in question["choices"]:
            assert choice["x_mm"] < 285
            assert choice["y_mm"] < 190


def test_vocabulary_sheet_infers_numbered_problem_ranges() -> None:
    questions = [
        Question(
            id=index,
            question_no=index,
            local_question_no=index,
            display_label=str(index),
            choice_count=4,
            display_order=index,
            item_type="goi",
        )
        for index in range(1, 71)
    ]

    coordinate_map = _coordinate_map_for_questions(questions, "文字・語彙")

    groups = coordinate_map["groups"]
    assert [group["group_label"] for group in groups] == [
        f"もんだい{index}" for index in range(1, 14)
    ]
    assert len(groups[0]["question_ids"]) == 6
    assert len(groups[1]["question_ids"]) == 7
    assert groups[-1]["question_ids"] == [69, 70]
    assert {question["group_label"] for question in coordinate_map["questions"]} >= {
        "もんだい1",
        "もんだい13",
    }


def test_listening_sheet_infers_numbered_problem_blocks_with_repeated_local_numbers() -> None:
    local_labels = [
        *range(1, 7),
        *range(1, 8),
        *range(1, 7),
        *range(1, 15),
        "1",
        "2",
        "3.1",
        "3.2",
    ]
    questions = [
        Question(
            id=index,
            question_no=index,
            display_label=str(label),
            choice_count=4,
            display_order=index,
            item_type="listening",
        )
        for index, label in enumerate(local_labels, start=1)
    ]

    coordinate_map = _coordinate_map_for_questions(questions, "聴解")

    groups = coordinate_map["groups"]
    assert [group["group_label"] for group in groups] == [
        "もんだい1",
        "もんだい2",
        "もんだい3",
        "もんだい4",
        "もんだい5",
    ]
    assert [len(group["question_ids"]) for group in groups] == [6, 7, 6, 14, 4]
    assert [question["display_label"] for question in coordinate_map["questions"][-4:]] == [
        "1",
        "2",
        "3.1",
        "3.2",
    ]


def test_overflowing_group_continues_without_header_or_continued_label() -> None:
    """A group that overflows into the next column continues with no repeated header.

    The first block keeps the group title; continuation blocks carry an empty label (no
    "continued" marker and no repeated もんだい title), so the table simply continues.
    """
    questions = [
        Question(
            id=index,
            question_no=index,
            display_label=str(index),
            choice_count=4,
            display_order=index,
            item_type="どっかい",
        )
        for index in range(1, 41)
    ]

    layout = calculate_sheet_layout(questions)

    labels = [group.group_label for group in layout.groups]
    # The group overflowed into at least one continuation block.
    assert len(layout.groups) >= 2
    # Exactly one labelled block (the first); the rest are headerless continuations.
    assert labels[0] == "どっかい"
    assert labels[1:] == ["" for _ in labels[1:]]
    assert not any("continued" in label for label in labels)


def test_sheet_generation_skips_empty_sheets_and_combines_roster_pdf() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(
            name="Sample Test / June",
            assessment_type="generic_test",
            exam_date="2026-06-07",
        )
        active_sheet = PhysicalSheet(
            sheet_no=1, sheet_name="文字・語彙", slug="language", display_order=1
        )
        empty_sheet = PhysicalSheet(
            sheet_no=2, sheet_name="空シート", slug="empty", display_order=2
        )
        section = ScoringSection(
            code="language",
            name="Language",
        )
        exam.physical_sheets.extend([active_sheet, empty_sheet])
        exam.scoring_sections.append(section)
        students = [
            Student(student_number="S003", name="Student C", class_name="B"),
            Student(student_number="S001", name="Student A", class_name="A"),
            Student(student_number="S002", name="Student B", class_name="A"),
        ]
        session.add(exam)
        session.add_all(students)
        session.commit()

        session.add(ExamStudent(exam_id=exam.id, student_id=students[0].id, seat_order=2))
        session.add(ExamStudent(exam_id=exam.id, student_id=students[1].id, seat_order=1))
        session.add(
            ExamStudent(
                exam_id=exam.id,
                student_id=students[2].id,
                seat_order=3,
                included=False,
            )
        )
        for index in range(1, 71):
            session.add(
                Question(
                    exam_id=exam.id,
                    physical_sheet_id=active_sheet.id,
                    scoring_section_id=section.id,
                    question_no=index,
                    problem_no=((index - 1) // 10) + 1,
                    local_question_no=((index - 1) % 10) + 1,
                    display_label=str(index),
                    correct_answer="1",
                    choice_count=4,
                    display_order=index,
                )
            )
        session.commit()

        summary = generate_exam_sheets(session, exam)

        assert summary.class_pdf_paths == [
            "generated_pdfs/exam_1/2026-06-07_Sample_Test_June_answer_sheets.pdf"
        ]
        assert summary.download_path == summary.class_pdf_paths[0]
        assert summary.zip_path is None
        assert (get_settings().data_root / summary.class_pdf_paths[0]).exists()
        document = open_document(get_settings().data_root / summary.class_pdf_paths[0])
        first_page_text = extract_text(document, 0)
        document.close()
        assert "Sample Test / June" in first_page_text
        assert "Sample" in first_page_text
        assert "文字・語彙" in first_page_text
        assert "OMR " + "Lite" not in first_page_text  # no legacy product name
        assert summary.sheet_instances_created == 2
        assert summary.templates_created == 1
        assert len(exam.sheet_templates) == 1
        assert len(exam.sheet_instances) == 2
        assert {instance.physical_sheet_id for instance in exam.sheet_instances} == {
            active_sheet.id
        }
        assert [instance.printed_student_number for instance in exam.sheet_instances] == [
            "S001",
            "S003",
        ]
        first_tokens = {
            (instance.student_id, instance.physical_sheet_id): instance.qr_token
            for instance in exam.sheet_instances
        }
        regenerated = generate_exam_sheets(session, exam)
        assert regenerated.download_path == summary.class_pdf_paths[0]
        assert regenerated.zip_path is None
        assert {
            (instance.student_id, instance.physical_sheet_id): instance.qr_token
            for instance in exam.sheet_instances
        } == first_tokens
        coordinate_map = exam.sheet_templates[0].coordinate_map_json
        assert coordinate_map["orientation"] == "landscape"
        assert len(coordinate_map["questions"]) == 70


def test_generation_fails_when_all_physical_sheets_have_zero_questions() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        exam = MockExam(name="Empty Exam", assessment_type="generic_test")
        student = Student(student_number="S001", name="Student A", class_name="A")
        exam.physical_sheets.append(
            PhysicalSheet(sheet_no=1, sheet_name="Empty", slug="empty", display_order=1)
        )
        session.add_all([exam, student])
        session.commit()
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
        session.commit()

        try:
            generate_exam_sheets(session, exam)
        except ValueError as exc:
            assert "Generate questions" in str(exc)
        else:
            raise AssertionError("Expected zero-question sheet generation to fail")
