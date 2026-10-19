"""Regression tests for the peer-review consistency cleanup pass.

Covers:
- the canonical ``PROBLEM_STATUSES`` driving both the review queue and the
  progress/problem counts, so ``reviewed_blank``/``reviewed_invalid`` never count as
  unresolved while ``blank``/``invalid`` still do (Task 1);
- the Scan & Review session state staying robust when peer-review activity lives only in
  ratings/scanned pages/results — a missing or stale batch counter must never report
  ``no_scans`` — while staying aligned with the workflow status (Tasks 2 & 3);
- normal-exam scan-review state behaviour staying unchanged (Task 2);
- manual student import detecting duplicates that differ only by whitespace (Task 4).
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.main import create_app
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewBatch,
    PeerReviewFinalResult,
    PeerReviewPage,
    PeerReviewRating,
    ScanBatch,
    Student,
)
from app.routes.dashboard import _session_state
from app.routes.peer_review import _peer_review_progress
from app.services.peer_review_builder_service import get_or_create_form
from app.services.peer_review_review_service import (
    PROBLEM_STATUSES,
    apply_rating_correction,
    build_review_context,
    list_problem_ratings,
)
from app.services.peer_review_sheet_service import generate_peer_review_pages
from app.services.peer_review_workflow_service import refresh_peer_review_exam_status
from app.services.review_service import count_reviewable

PROBLEM_SAMPLE = ("blank", "invalid", "unclear", "multiple_mark")
RESOLVED_SAMPLE = ("ok", "reviewed", "reviewed_blank", "reviewed_invalid")


def _engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return engine


def _peer_review_exam(session: Session) -> tuple[MockExam, list[Student]]:
    exam = MockExam(name="Presentation Review", assessment_type="peer_review")
    students = [
        Student(student_number="001", name="Yamada Taro", class_name="3A"),
        Student(student_number="002", name="Tanaka Hanako", class_name="3A"),
    ]
    session.add_all([exam, *students])
    session.flush()
    for index, student in enumerate(students, start=1):
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id, seat_order=index))
    session.add(Competency(exam_id=exam.id, name="Clarity", display_order=1))
    session.commit()
    return exam, students


def _peer_form_with_page(session: Session):
    """A peer-review session with a generated page/targets/rater group but no scan batch."""
    exam, students = _peer_review_exam(session)
    form = get_or_create_form(session, exam)
    page = generate_peer_review_pages(session, exam)[0]
    comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
    return exam, students, form, page, comp_id


def _add_rating(
    session: Session,
    exam: MockExam,
    form,
    page: PeerReviewPage,
    student_id: int,
    comp_id: int,
    status: str,
    *,
    final_score: int | None = None,
    submission_page_id: int = 0,
) -> PeerReviewRating:
    """Add a rating directly (no PeerReviewBatch) to exercise the dedicated-table state."""
    rating = PeerReviewRating(
        exam_id=exam.id,
        form_id=form.id,
        batch_id=0,
        rater_group_id=form.rater_groups[0].id,
        submission_page_id=submission_page_id,
        peer_review_page_id=page.id,
        target_student_id=student_id,
        competency_id=comp_id,
        detected_score=None,
        final_score=final_score,
        status=status,
    )
    session.add(rating)
    session.commit()
    return rating


# --- Task 1: one shared unresolved-status definition -----------------------------


def test_progress_problem_count_matches_review_queue() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        for status in (*PROBLEM_SAMPLE, *RESOLVED_SAMPLE):
            _add_rating(session, exam, form, page, students[0].id, comp_id, status)

        progress = _peer_review_progress(session, form.id)
        problems = list_problem_ratings(session, form)
        # The progress counter, the review queue, and the dashboard reviewable count must
        # all agree on what "unresolved" means.
        assert progress["total_ratings"] == 8
        assert progress["needs_review"] == len(problems) == len(PROBLEM_SAMPLE)
        assert count_reviewable(session, exam.id) == len(PROBLEM_SAMPLE)


def test_reviewed_blank_and_invalid_are_not_problem_counts() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        _add_rating(session, exam, form, page, students[0].id, comp_id, "reviewed_blank")
        _add_rating(session, exam, form, page, students[1].id, comp_id, "reviewed_invalid")

        # Canonical definition never treats a confirmed resolution as a problem.
        assert "reviewed_blank" not in PROBLEM_STATUSES
        assert "reviewed_invalid" not in PROBLEM_STATUSES
        progress = _peer_review_progress(session, form.id)
        assert progress["needs_review"] == 0
        assert progress["ready_to_score"] is True
        assert count_reviewable(session, exam.id) == 0
        assert build_review_context(session, form)["problem_count"] == 0


def test_blank_and_invalid_still_count_before_review() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        _add_rating(session, exam, form, page, students[0].id, comp_id, "blank")
        _add_rating(session, exam, form, page, students[1].id, comp_id, "invalid")

        progress = _peer_review_progress(session, form.id)
        assert progress["needs_review"] == 2
        assert progress["ready_to_score"] is False
        assert count_reviewable(session, exam.id) == 2
        assert len(list_problem_ratings(session, form)) == 2


# --- Task 2: scan-review state robust without a batch counter ---------------------


def test_scan_state_resolved_ratings_without_batch_is_ready_to_score() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        _add_rating(session, exam, form, page, students[0].id, comp_id, "reviewed", final_score=4)
        _add_rating(session, exam, form, page, students[1].id, comp_id, "reviewed", final_score=5)
        assert session.query(PeerReviewBatch).filter_by(form_id=form.id).count() == 0

        state = _session_state(session, exam)
        # Ratings exist, so a zero batch count must not report "no scans".
        assert state["state"] != "no_scans"
        assert state["state"] == "ready_to_score"
        assert state["upload_url"].endswith("/peer-review/scan-batches")
        assert state["review_url"].endswith("/peer-review/review")
        assert state["results_url"].endswith(f"/exams/{exam.id}/results")


def test_scan_state_scanned_pages_without_batch_not_no_scans() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        # Two distinct scanned/submission pages referenced by ratings, but no batch row.
        _add_rating(
            session, exam, form, page, students[0].id, comp_id, "blank", submission_page_id=11
        )
        _add_rating(
            session, exam, form, page, students[1].id, comp_id, "blank", submission_page_id=12
        )
        assert session.query(PeerReviewBatch).filter_by(form_id=form.id).count() == 0

        state = _session_state(session, exam)
        assert state["state"] != "no_scans"
        # Unresolved ratings still route to review.
        assert state["state"] == "needs_review"
        assert state["uploaded_pages"] >= 1


def test_scan_state_reviewed_blank_invalid_only_is_not_needs_review() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        _add_rating(session, exam, form, page, students[0].id, comp_id, "reviewed_blank")
        _add_rating(session, exam, form, page, students[1].id, comp_id, "reviewed_invalid")

        state = _session_state(session, exam)
        assert state["state"] != "needs_review"
        assert state["state"] == "ready_to_score"


def test_scan_state_with_results_is_results_ready() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        _add_rating(session, exam, form, page, students[0].id, comp_id, "reviewed", final_score=4)
        session.add(
            PeerReviewFinalResult(
                exam_id=exam.id,
                form_id=form.id,
                target_student_id=students[0].id,
                final_score=4.0,
            )
        )
        session.commit()

        state = _session_state(session, exam)
        assert state["state"] == "results_ready"
        assert state["results_url"].endswith(f"/exams/{exam.id}/results")


def test_normal_exam_scan_state_unchanged() -> None:
    with Session(_engine()) as session:
        exam = MockExam(name="Standard Test", assessment_type="generic_test", status="printed")
        session.add(exam)
        session.commit()

        # No scan batches -> no_scans, and the URLs stay on the normal (non peer-review) routes.
        state = _session_state(session, exam)
        assert state["state"] == "no_scans"
        assert state["upload_url"].endswith(f"/exams/{exam.id}/scan-batches")
        assert "/peer-review/" not in state["upload_url"]

        session.add(
            ScanBatch(
                exam_id=exam.id,
                uploaded_file_name="scan.pdf",
                uploaded_file_path="uploaded_scans/scan.pdf",
                total_pages=3,
                status="processed",
            )
        )
        session.commit()
        session.refresh(exam)

        # Batch present, nothing to review, no results -> not_calculated (unchanged behaviour).
        state = _session_state(session, exam)
        assert state["uploaded_pages"] == 3
        assert state["state"] == "not_calculated"


# --- Task 3: workflow status and scan-review state stay aligned ------------------


def test_workflow_status_and_scan_state_stay_aligned() -> None:
    with Session(_engine()) as session:
        exam, students, form, page, comp_id = _peer_form_with_page(session)
        _add_rating(session, exam, form, page, students[0].id, comp_id, "blank")
        _add_rating(session, exam, form, page, students[1].id, comp_id, "blank")

        # Unresolved ratings -> both the workflow status and the overview state say review.
        assert refresh_peer_review_exam_status(session, exam, form) == "needs_review"
        assert _session_state(session, exam)["state"] == "needs_review"

        for rating in list_problem_ratings(session, form):
            apply_rating_correction(
                session,
                rating,
                score=3,
                status="reviewed",
                score_min=form.score_min,
                score_max=form.score_max,
            )

        # Resolved, no results yet -> both say ready_to_score (never "no_scans").
        assert refresh_peer_review_exam_status(session, exam, form) == "ready_to_score"
        assert _session_state(session, exam)["state"] == "ready_to_score"


# --- Task 4: whitespace-insensitive duplicate detection in manual import ----------


def _client() -> TestClient:
    return TestClient(create_app())


def _generic_run(client: TestClient) -> int:
    response = client.post(
        "/exams",
        data={"name": "Generic Test", "assessment_type": "generic_test"},
        follow_redirects=False,
    )
    return int(response.headers["location"].split("/")[2])


def test_manual_import_detects_whitespace_only_duplicate_in_form() -> None:
    with _client() as client:
        run_id = _generic_run(client)
        # Two rows whose numbers differ only by an internal space -> rejected as a duplicate.
        response = client.post(
            f"/exams/{run_id}/students/add-manual",
            data={
                "student_number": ["CLEANUP-SYN1", "CLEANUP- SYN1"],
                "name": ["Alice", "Bob"],
                "class_name": ["3A", "3A"],
            },
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert client.get(f"/exams/{run_id}/students").text.count("CLEANUP-SYN1") == 0


def test_manual_import_detects_whitespace_only_duplicate_in_session() -> None:
    with _client() as client:
        run_id = _generic_run(client)
        first = client.post(
            f"/exams/{run_id}/students/add-manual",
            data={"student_number": "CLEANUP-SYN1", "name": "Alice", "class_name": "3A"},
            follow_redirects=False,
        )
        assert first.status_code == 303

        # A full-width space in the same number is still the same student.
        response = client.post(
            f"/exams/{run_id}/students/add-manual",
            data={"student_number": "CLEANUP-　SYN1", "name": "Bob", "class_name": "3A"},
            follow_redirects=False,
        )
        assert response.status_code == 400
