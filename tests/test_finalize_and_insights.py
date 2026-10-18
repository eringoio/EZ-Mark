"""Result finalization and Insights workflow-state coverage.

Resolving the last review item must make the session results-ready without a
manual recalculate, and the Insights overview must label unfinished sessions.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import (
    ExamStudent,
    MockExam,
    PhysicalSheet,
    ProblemGroup,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    ScoringSection,
    SheetInstance,
    SheetTemplate,
    Student,
)
from app.routes.dashboard import _session_state
from app.services.review_service import correct_response
from app.services.scoring_service import maybe_finalize_exam, question_analysis


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        yield db


def _scanned_exam(session: Session, response_status: str = "unclear") -> dict:
    exam = MockExam(name="Synthetic Test", assessment_type="generic_test")
    student = Student(student_number="SYN001", name="Student A", class_name="A")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="文字・語彙", slug="vocabulary", display_order=1)
    section = ScoringSection(code="language_knowledge", name="LK")
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    session.add_all([exam, student])
    session.commit()
    session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
    group = ProblemGroup(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        problem_no=1,
        problem_title="もんだい1",
        subquestion_count=1,
        display_order=1,
    )
    question = Question(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        scoring_section_id=section.id,
        question_no=1,
        problem_no=1,
        subquestion_no=1,
        display_label="もんだい1-問1",
        choice_count=4,
        correct_answer="2",
    )
    template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
    session.add_all([group, question, template])
    session.flush()
    instance = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="opaque-token",
        printed_name=student.name,
        printed_student_number=student.student_number,
    )
    batch = ScanBatch(
        exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x", total_pages=1
    )
    page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
    response = Response(
        scanned_page=page,
        question=question,
        detected_answer=None,
        final_answer=None,
        status=response_status,
    )
    session.add_all([instance, batch, page, response])
    session.commit()
    return {"exam": exam, "question": question, "response": response}


def test_unresolved_review_keeps_exam_in_needs_review(session) -> None:
    data = _scanned_exam(session)

    finalized = maybe_finalize_exam(session, data["exam"])

    assert finalized is False
    assert data["exam"].status == "needs_review"
    assert _session_state(session, data["exam"])["state"] == "needs_review"


def test_resolving_last_review_item_finalizes_results(session) -> None:
    data = _scanned_exam(session)
    maybe_finalize_exam(session, data["exam"])

    correct_response(session, data["response"].id, "2")
    finalized = maybe_finalize_exam(session, data["exam"])

    assert finalized is True
    assert data["exam"].status == "results_ready"
    assert len(data["exam"].results) == 1
    assert _session_state(session, data["exam"])["state"] == "results_ready"


def test_session_state_without_scans(session) -> None:
    exam = MockExam(name="Fresh", assessment_type="generic_test")
    session.add(exam)
    session.commit()

    assert _session_state(session, exam)["state"] == "no_scans"


def test_question_analysis_labels_include_sheet_and_problem_group(session) -> None:
    data = _scanned_exam(session, response_status="ok")
    data["response"].final_answer = "2"
    session.commit()

    rows = question_analysis(session, data["exam"])

    assert rows[0]["sheet"].sheet_name == "文字・語彙"
    assert rows[0]["group_label"] == "もんだい1"
    assert rows[0]["q_label"] == "問1"
    assert rows[0]["correct_count"] == 1
