from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import (
    ExamStudent,
    MockExam,
    PhysicalSheet,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    ScoringSection,
    SheetInstance,
    SheetTemplate,
    Student,
)
from app.services.item_type_analysis_service import (
    calculate_exam_item_type_breakdowns,
    calculate_student_item_type_breakdown,
)

# (item_type, correct_answer) for the six exam questions; last has no item type.
QUESTION_SPECS = [
    ("moji", "1"),
    ("goi", "2"),
    ("bunpou", "3"),
    ("reading", "4"),
    ("listening", "1"),
    (None, "2"),
]


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _build_exam(session: Session):
    exam = MockExam(name="Item Sample", assessment_type="generic_test")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="S", slug="s", display_order=1)
    section = ScoringSection(code="language_knowledge", name="LK")
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    session.add(exam)
    session.commit()
    template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
    session.add(template)
    session.flush()
    for index, (item_type, answer) in enumerate(QUESTION_SPECS, start=1):
        session.add(
            Question(
                exam_id=exam.id,
                physical_sheet_id=sheet.id,
                scoring_section_id=section.id,
                display_label=str(index),
                choice_count=4,
                correct_answer=answer,
                item_type=item_type,
                display_order=index,
            )
        )
    session.commit()
    return exam, sheet, template


def _add_student(session, exam, sheet, template, number, answers) -> Student:
    """answers: list of (status, final_answer, detected_answer) aligned to QUESTION_SPECS."""
    student = Student(student_number=number, name=number, class_name="A")
    session.add(student)
    session.commit()
    session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
    instance = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token=f"tok-{number}",
        printed_name=student.name,
        printed_student_number=number,
    )
    batch = ScanBatch(exam_id=exam.id, uploaded_file_name="s.pdf", uploaded_file_path="x")
    page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
    session.add_all([instance, batch, page])
    session.flush()
    questions = sorted(exam.questions, key=lambda q: q.display_order)
    for question, (status, final_answer, detected) in zip(questions, answers, strict=True):
        session.add(
            Response(
                scanned_page=page,
                question=question,
                detected_answer=detected,
                final_answer=final_answer,
                status=status,
            )
        )
    session.commit()
    return student


# moji correct; goi wrong; bunpou blank; reading multiple; listening reviewed-correct
# (detected differs); unknown correct.
ANSWERS = [
    ("ok", "1", "1"),
    ("ok", "3", "3"),
    ("blank", None, None),
    ("multiple_mark", None, None),
    ("ok", "1", "9"),
    ("ok", "2", "2"),
]


def test_breakdown_for_mixed_item_types() -> None:
    with _session() as session:
        exam, sheet, template = _build_exam(session)
        student = _add_student(session, exam, sheet, template, "S1", ANSWERS)

        breakdown = calculate_student_item_type_breakdown(session, exam, student.id)

        assert list(breakdown) == ["moji", "goi", "bunpou", "reading", "listening", "unknown"]
        assert breakdown["moji"]["correct"] == 1
        assert breakdown["moji"]["total"] == 1
        assert breakdown["moji"]["percentage"] == 100.0
        assert breakdown["moji"]["label"] == "moji"
        assert breakdown["goi"]["correct"] == 0
        assert breakdown["goi"]["accuracy"] == 0.0


def test_reviewed_final_answer_is_used_not_detected() -> None:
    with _session() as session:
        exam, sheet, template = _build_exam(session)
        student = _add_student(session, exam, sheet, template, "S1", ANSWERS)

        breakdown = calculate_student_item_type_breakdown(session, exam, student.id)

        # listening detected "9" (wrong) but reviewed final_answer "1" (correct).
        assert breakdown["listening"]["correct"] == 1


def test_blank_and_multiple_count_as_incorrect() -> None:
    with _session() as session:
        exam, sheet, template = _build_exam(session)
        student = _add_student(session, exam, sheet, template, "S1", ANSWERS)

        breakdown = calculate_student_item_type_breakdown(session, exam, student.id)

        assert breakdown["bunpou"]["correct"] == 0  # blank
        assert breakdown["reading"]["correct"] == 0  # multiple mark


def test_missing_item_type_grouped_as_unknown() -> None:
    with _session() as session:
        exam, sheet, template = _build_exam(session)
        student = _add_student(session, exam, sheet, template, "S1", ANSWERS)

        breakdown = calculate_student_item_type_breakdown(session, exam, student.id)

        assert "unknown" in breakdown
        assert breakdown["unknown"]["total"] == 1
        assert breakdown["unknown"]["correct"] == 1
        assert breakdown["unknown"]["label"] == "unknown"


def test_exam_level_breakdown_one_per_student() -> None:
    with _session() as session:
        exam, sheet, template = _build_exam(session)
        s1 = _add_student(session, exam, sheet, template, "S1", ANSWERS)
        all_wrong = [("ok", "5", "5")] * len(QUESTION_SPECS)
        s2 = _add_student(session, exam, sheet, template, "S2", all_wrong)

        breakdowns = calculate_exam_item_type_breakdowns(session, exam)

        assert set(breakdowns) == {s1.id, s2.id}
        assert breakdowns[s1.id]["moji"]["correct"] == 1
        assert breakdowns[s2.id]["moji"]["correct"] == 0
