"""Standard Test scoring coverage: canonical points, fractional totals, explicit pass marks.

These tests exercise the generic (Standard Test / Multiple-Choice) workflow at the
service level: ``points`` is the one canonical weight, totals keep fractional values
(7.5 stays 7.5), and pass/fail only appears when an explicit pass mark is set.
"""

import math

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base, SessionLocal
from app.main import create_app
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
from app.services.export_service import _student_summary_headers, _student_summary_row
from app.services.scoring_service import format_score, recalculate_exam_results


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return Session(engine)


def _run(session: Session, *, pass_mark_value: float | None = None, pass_mark_type=None) -> tuple:
    """One Standard Test run with a student, one required sheet, and an answer key."""
    exam = MockExam(
        name="Standard Test",
        assessment_type="generic_test",
        pass_mark_value=pass_mark_value,
        pass_mark_type=pass_mark_type,
    )
    sheet = PhysicalSheet(sheet_no=1, sheet_name="Sheet", slug="sheet", display_order=1)
    section = ScoringSection(code="part_a", name="Part A", display_order=1)
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    student = Student(student_number="STD001", name="Student A", class_name="A")
    session.add_all([exam, student])
    session.commit()
    session.add(ExamStudent(exam_id=exam.id, student_id=student.id))
    template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
    session.add(template)
    session.flush()
    return exam, sheet, section, student, template


def _print_sheet(session: Session, exam, student, sheet, template):
    instance = SheetInstance(
        exam_id=exam.id,
        student_id=student.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="opaque-token",
        printed_name=student.name,
        printed_student_number=student.student_number,
    )
    batch = ScanBatch(exam_id=exam.id, uploaded_file_name="scan.pdf", uploaded_file_path="x")
    page = ScannedPage(scan_batch=batch, sheet_instance=instance, page_no=1, image_path="x")
    session.add_all([instance, batch, page])
    session.flush()
    return page


def _question(session, exam, sheet, section, label, answer, points=1.0, choices=4):
    question = Question(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        scoring_section_id=section.id,
        display_label=label,
        choice_count=choices,
        correct_answer=answer,
        points=points,
        display_order=int(label),
    )
    session.add(question)
    session.flush()
    return question


def _answer(session, page, question, final_answer, status="ok"):
    response = Response(
        scanned_page=page, question=question, final_answer=final_answer, status=status
    )
    session.add(response)
    session.flush()
    return response


def test_twenty_questions_one_point_each() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(
        session, pass_mark_value=10, pass_mark_type="points"
    )
    page = _print_sheet(session, exam, student, sheet, template)
    for index in range(20):
        question = _question(session, exam, sheet, section, str(index + 1), "2")
        _answer(session, page, question, "2" if index < 10 else "3")  # first 10 correct
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 10
    assert results[0].score_details_json["max_points"] == 20.0
    assert results[0].score_details_json["percentage"] == 50.0
    assert results[0].pass_fail_status == "pass"


def test_mixed_point_values() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(session)
    page = _print_sheet(session, exam, student, sheet, template)
    q1 = _question(session, exam, sheet, section, "1", "1", points=3.0)
    q2 = _question(session, exam, sheet, section, "2", "2", points=1.0)
    _answer(session, page, q1, "1")
    _answer(session, page, q2, "3")  # wrong
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 3
    assert results[0].score_details_json["max_points"] == 4.0
    assert results[0].score_details_json["percentage"] == 75.0


def test_fractional_points_preserved() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(
        session, pass_mark_value=7.5, pass_mark_type="points"
    )
    page = _print_sheet(session, exam, student, sheet, template)
    q1 = _question(session, exam, sheet, section, "1", "2", points=5.0)
    q2 = _question(session, exam, sheet, section, "2", "1", points=2.5)
    _answer(session, page, q1, "2")  # +5.0
    _answer(session, page, q2, "1")  # +2.5
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 7.5
    assert math.isclose(results[0].score_details_json["total"], 7.5)
    assert results[0].pass_fail_status == "pass"

    # Drop the 2.5-point question's response; the rest no longer reaches 7.5.
    session.execute(delete(Response).where(Response.question_id == q2.id))
    session.commit()
    results = recalculate_exam_results(session, exam)
    assert results[0].total_score == 5.0
    assert results[0].pass_fail_status == "fail"


def test_fractional_total_never_rounds_up() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(session)
    page = _print_sheet(session, exam, student, sheet, template)
    q1 = _question(session, exam, sheet, section, "1", "1", points=7.0)
    q2 = _question(session, exam, sheet, section, "2", "2", points=5.0)
    _answer(session, page, q1, "1")  # +7.0
    _answer(session, page, q2, "1")  # wrong, 0 of 5
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 7.0
    assert results[0].score_details_json["percentage"] == 58.3


def test_zero_point_question_allowed() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(session)
    page = _print_sheet(session, exam, student, sheet, template)
    q1 = _question(session, exam, sheet, section, "1", "1", points=0.0)
    q2 = _question(session, exam, sheet, section, "2", "1", points=2.0)
    _answer(session, page, q1, "5")  # wrong but worth nothing
    _answer(session, page, q2, "1")  # correct
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 2.0
    assert results[0].score_details_json["max_points"] == 2.0


def test_choice_counts_two_to_five() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(session)
    page = _print_sheet(session, exam, student, sheet, template)
    for choice_count in (2, 3, 4, 5):
        question = _question(
            session,
            exam,
            sheet,
            section,
            str(choice_count),
            str(choice_count),
            choices=choice_count,
        )
        _answer(session, page, question, str(choice_count))
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 4.0
    assert results[0].pass_fail_status == "scored"  # no pass mark configured


def test_point_pass_mark_threshold() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(
        session, pass_mark_value=15, pass_mark_type="points"
    )
    page = _print_sheet(session, exam, student, sheet, template)
    for index in range(20):
        question = _question(session, exam, sheet, section, str(index + 1), "2")
        _answer(session, page, question, "2" if index < 14 else "3")
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 14
    assert results[0].pass_fail_status == "fail"  # 14 points < the 15-point threshold


def test_percentage_pass_mark_threshold() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(
        session, pass_mark_value=70, pass_mark_type="percentage"
    )
    page = _print_sheet(session, exam, student, sheet, template)
    q1 = _question(session, exam, sheet, section, "1", "1", points=2.0)
    q2 = _question(session, exam, sheet, section, "2", "1", points=8.0)
    _answer(session, page, q1, "1")  # +2 of 10 = 20%
    _answer(session, page, q2, "2")  # wrong
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].score_details_json["percentage"] == 20.0
    assert results[0].pass_fail_status == "fail"


def test_no_pass_mark_neutral_status() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(session)
    page = _print_sheet(session, exam, student, sheet, template)
    question = _question(session, exam, sheet, section, "1", "1")
    _answer(session, page, question, "5")  # wrong, but still just "scored"
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 0
    assert results[0].pass_fail_status == "scored"


def test_incomplete_scan_missing_required_sheet() -> None:
    session = _session()
    exam, sheet, section, _student, _template = _run(session)
    _question(session, exam, sheet, section, "1", "1")
    session.commit()
    # No scanned page at all: the required sheet is missing.

    results = recalculate_exam_results(session, exam)

    assert results[0].pass_fail_status == "incomplete"
    assert results[0].total_score == 0


def test_unreviewed_marks_block_scoring() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(session)
    page = _print_sheet(session, exam, student, sheet, template)
    question = _question(session, exam, sheet, section, "1", "1")
    _answer(session, page, question, None, status="unclear")
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].pass_fail_status == "not_scored"
    assert results[0].total_score == 0


def test_manual_correction_then_recalculation() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(
        session, pass_mark_value=10, pass_mark_type="percentage"
    )
    page = _print_sheet(session, exam, student, sheet, template)
    q1 = _question(session, exam, sheet, section, "1", "1")
    q2 = _question(session, exam, sheet, section, "2", "2")
    r1 = _answer(session, page, q1, None, status="unclear")
    _answer(session, page, q2, "2")
    session.commit()

    results = recalculate_exam_results(session, exam)
    assert results[0].pass_fail_status == "not_scored"

    # Staff confirm the first mark during review (wrong answer, response resolved).
    r1.final_answer = "2"
    r1.status = "ok"
    session.commit()

    results = recalculate_exam_results(session, exam)

    assert results[0].total_score == 1.0
    assert results[0].score_details_json["percentage"] == 50.0
    assert results[0].pass_fail_status == "pass"  # 50% >= the 10% pass mark


def test_section_breakdown_uses_points_not_scaled_max() -> None:
    session = _session()
    exam, sheet, section_a, student, template = _run(session)
    section_b = ScoringSection(code="part_b", name="Part B", display_order=2)
    exam.scoring_sections.append(section_b)
    session.flush()
    page = _print_sheet(session, exam, student, sheet, template)
    a1 = _question(session, exam, sheet, section_a, "1", "1", points=3.0)
    b1 = _question(session, exam, sheet, section_b, "2", "1", points=7.0)
    _answer(session, page, a1, "1")
    _answer(session, page, b1, "5")  # wrong
    session.commit()

    results = recalculate_exam_results(session, exam)

    details = results[0].score_details_json
    assert details["part_a"] == {"earned": 3.0, "possible": 3.0, "percentage": 100.0}
    assert details["part_b"] == {"earned": 0.0, "possible": 7.0, "percentage": 0.0}
    # Possible points are the raw question points; no arbitrary scaled maximum.
    assert details["total"] == 3.0
    assert details["max_points"] == 10.0
    assert results[0].raw_json["part_b"] == {"correct": 0.0, "total": 7.0, "accuracy": 0.0}


def test_export_preserves_fractional_scores_and_pass_mark() -> None:
    session = _session()
    exam, sheet, section, student, template = _run(
        session, pass_mark_value=7.5, pass_mark_type="points"
    )
    page = _print_sheet(session, exam, student, sheet, template)
    q1 = _question(session, exam, sheet, section, "1", "1", points=2.5)
    q2 = _question(session, exam, sheet, section, "2", "1", points=5.0)
    _answer(session, page, q1, "1")
    _answer(session, page, q2, "1")
    session.commit()
    results = recalculate_exam_results(session, exam)

    headers = _student_summary_headers(exam)
    row = _student_summary_row(exam, results[0])

    assert "pass_mark_value" in headers and "pass_mark_type" in headers
    assert row["total_score"] == 7.5
    assert row["pass_mark_value"] == 7.5
    assert row["pass_mark_type"] == "points"
    assert row["part_a_score"] == 7.5


def test_format_score_display() -> None:
    assert format_score(None) == ""
    assert format_score(7.0) == "7"
    assert format_score(7.5) == "7.5"
    assert format_score(7) == "7"


def test_results_pass_mark_route_sets_types_and_clears() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/templates/new",
            data={"name": "PassMark Route", "assessment_type": "generic_test"},
            follow_redirects=True,
        )
        template_id = int(response.url.path.rstrip("/").split("/")[-2])
        response = client.post(
            f"/templates/{template_id}/runs/new",
            data={"run_name": "PassMark Run", "exam_date": ""},
            follow_redirects=True,
        )
        run_id = int(response.url.path.rstrip("/").split("/")[2])

        percentage = client.post(
            f"/exams/{run_id}/results/pass-mark",
            data={"pass_mark_type": "percentage", "pass_mark_value": "70"},
            follow_redirects=False,
        )
        assert percentage.status_code == 303
        with SessionLocal() as session:
            exam = session.get(MockExam, run_id)
            assert exam.pass_mark_type == "percentage"
            assert exam.pass_mark_value == 70.0
            exam_id = exam.id

        points = client.post(
            f"/exams/{run_id}/results/pass-mark",
            data={"pass_mark_type": "points", "pass_mark_value": "7.5"},
            follow_redirects=False,
        )
        assert points.status_code == 303
        with SessionLocal() as session:
            exam = session.get(MockExam, exam_id)
            assert exam.pass_mark_type == "points"
            assert exam.pass_mark_value == 7.5

        cleared = client.post(
            f"/exams/{run_id}/results/pass-mark",
            data={"pass_mark_type": "", "pass_mark_value": ""},
            follow_redirects=False,
        )
        assert cleared.status_code == 303
        with SessionLocal() as session:
            exam = session.get(MockExam, exam_id)
            assert exam.pass_mark_type is None
            assert exam.pass_mark_value is None
