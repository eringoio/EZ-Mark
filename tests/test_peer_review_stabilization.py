"""Regression tests for the peer-review stabilization pass.

Covers: resolved blank/invalid statuses leaving the review queue, recalculation after
reviewed_blank/reviewed_invalid exist, review context scoped to the current form, the
centralized workflow-status refresh, and scan-review visibility/page counts for
peer-review sessions (whose scans live in dedicated tables, not exam.scan_batches).
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewFinalResult,
    PeerReviewRating,
    Student,
)
from app.routes.dashboard import (
    _peer_review_has_scan_activity,
    _peer_review_scanned_page_count,
    _session_state,
)
from app.services.peer_review_builder_service import get_or_create_form
from app.services.peer_review_review_service import (
    apply_rating_correction,
    build_review_context,
    list_problem_ratings,
)
from app.services.peer_review_scan_service import (
    create_peer_review_batch,
    record_submission_page,
)
from app.services.peer_review_scoring_service import (
    recalculate_anonymous_peer_review_results,
)
from app.services.peer_review_sheet_service import generate_peer_review_pages
from app.services.peer_review_workflow_service import refresh_peer_review_exam_status


def _engine() -> "create_engine":
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


def _seed_two_problem_ratings(session: Session):
    """Two unreadable (blank) ratings -> two review-queue items."""
    exam, students = _peer_review_exam(session)
    form = get_or_create_form(session, exam)
    page = generate_peer_review_pages(session, exam)[0]
    comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
    batch = create_peer_review_batch(
        session, form.id, form.rater_groups[0].id, "scan.pdf", "uploaded_scans/scan.pdf"
    )
    record_submission_page(
        session,
        batch,
        page,
        1,
        "processed_pages/a.png",
        {(students[0].id, comp_id): None, (students[1].id, comp_id): None},
    )
    return exam, students, form, comp_id


def test_manual_blank_leaves_queue() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        problems = list_problem_ratings(session, form)
        assert len(problems) == 2

        apply_rating_correction(
            session,
            problems[0],
            score=None,
            status="blank",
            score_min=form.score_min,
            score_max=form.score_max,
        )
        rating = session.get(PeerReviewRating, problems[0].id)
        assert rating.status == "reviewed_blank"
        assert rating.final_score is None
        assert len(list_problem_ratings(session, form)) == 1


def test_manual_invalid_leaves_queue() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        problems = list_problem_ratings(session, form)

        apply_rating_correction(
            session,
            problems[0],
            score=None,
            status="invalid",
            score_min=form.score_min,
            score_max=form.score_max,
        )
        rating = session.get(PeerReviewRating, problems[0].id)
        assert rating.status == "reviewed_invalid"
        assert rating.final_score is None
        assert len(list_problem_ratings(session, form)) == 1


def test_manual_score_correction_leaves_queue() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        problems = list_problem_ratings(session, form)

        apply_rating_correction(
            session,
            problems[0],
            score=4,
            status="reviewed",
            score_min=form.score_min,
            score_max=form.score_max,
        )
        rating = session.get(PeerReviewRating, problems[0].id)
        assert rating.status == "reviewed"
        assert rating.final_score == 4
        assert len(list_problem_ratings(session, form)) == 1


def test_final_correction_makes_queue_zero() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        for rating in list_problem_ratings(session, form):
            apply_rating_correction(
                session,
                rating,
                score=3,
                status="reviewed",
                score_min=form.score_min,
                score_max=form.score_max,
            )
        assert build_review_context(session, form)["problem_count"] == 0


def test_scoring_ignores_reviewed_blank_and_invalid() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        problems = list_problem_ratings(session, form)
        # One scored, one confirmed blank (non-scoring).
        apply_rating_correction(
            session,
            problems[0],
            score=5,
            status="reviewed",
            score_min=form.score_min,
            score_max=form.score_max,
        )
        apply_rating_correction(
            session,
            problems[1],
            score=None,
            status="blank",
            score_min=form.score_min,
            score_max=form.score_max,
        )
        recalculate_anonymous_peer_review_results(session, form)
        from app.models import PeerReviewGroupResult

        group_results = {
            r.target_student_id: r
            for r in session.scalars(
                select(PeerReviewGroupResult).where(PeerReviewGroupResult.form_id == form.id)
            )
        }
        # The scored student's group average reflects the single score of 5; the
        # confirmed-blank student contributes nothing (reviewed_blank is non-scoring).
        assert group_results[students[0].id].group_average_score == 5
        assert group_results[students[1].id].group_average_score is None


def test_review_context_scoped_to_form() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        # Unrelated student in a different exam must never appear in the review context.
        other_exam = MockExam(name="Other", assessment_type="peer_review")
        outsider = Student(student_number="999", name="Outsider", class_name="9Z")
        session.add_all([other_exam, outsider])
        session.commit()

        context = build_review_context(session, form)
        queue_targets = {item["target"] for item in context["queue"]}
        assert all("Outsider" not in label for label in queue_targets)


def test_refresh_status_ready_to_score_needs_review_results() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        # Two blank ratings -> needs_review.
        assert refresh_peer_review_exam_status(session, exam, form) == "needs_review"

        for rating in list_problem_ratings(session, form):
            apply_rating_correction(
                session,
                rating,
                score=3,
                status="reviewed",
                score_min=form.score_min,
                score_max=form.score_max,
            )
        # All resolved, no results yet -> ready_to_score.
        assert refresh_peer_review_exam_status(session, exam, form) == "ready_to_score"

        recalculate_anonymous_peer_review_results(session, form)
        session.add(
            PeerReviewFinalResult(
                exam_id=exam.id,
                form_id=form.id,
                target_student_id=students[0].id,
                final_score=3.0,
            )
        )
        session.commit()
        assert refresh_peer_review_exam_status(session, exam, form) == "results_ready"


def test_scanned_page_count_robust_when_batch_counter_stale() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        # Force the batch counter stale; submission pages + ratings still exist.
        from app.models import PeerReviewBatch

        for batch in session.scalars(
            select(PeerReviewBatch).where(PeerReviewBatch.form_id == form.id)
        ):
            batch.total_pages = 0
        session.commit()
        assert _peer_review_scanned_page_count(session, form.id) >= 1


def test_scan_review_visibility_for_peer_review_with_batch() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        assert _peer_review_has_scan_activity(session, exam) is True
        state = _session_state(session, exam)
        assert state["upload_url"].endswith("/peer-review/scan-batches")
        assert state["review_url"].endswith("/peer-review/review")
        assert state["uploaded_pages"] >= 1


def test_target_labels_relabel_on_sync_without_moving_bubbles() -> None:
    from app.models import PeerReviewTarget
    from app.services.peer_review_builder_service import (
        sync_targets_from_roster,
    )

    with Session(_engine()) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        bubbles_before = page.coordinate_map_json["rating_bubbles"]

        # Simulate stale student-code labels left by an older build.
        for target in form.targets:
            target.display_label = f"{target.student.student_number} {target.student.name}"
        for p in form.pages:
            rows = [dict(r) for r in p.target_rows_json]
            for r in rows:
                r["display_label"] = "stale-code-label"
            p.target_rows_json = rows
        session.commit()

        sync_targets_from_roster(session, form, raise_on_locked=False)
        session.refresh(form)

        targets = session.scalars(
            select(PeerReviewTarget).where(PeerReviewTarget.form_id == form.id)
        ).all()
        # Order number + name; no student code.
        assert {t.display_label for t in targets} == {"1. Yamada Taro", "2. Tanaka Hanako"}
        for t in targets:
            assert t.student.student_number not in t.display_label
        # Page rows refreshed too.
        page_labels = {row["display_label"] for p in form.pages for row in p.target_rows_json}
        assert "stale-code-label" not in page_labels
        assert page_labels == {"1. Yamada Taro", "2. Tanaka Hanako"}
        # Bubble coordinate map positions are unchanged.
        assert (
            session.get(type(page), page.id).coordinate_map_json["rating_bubbles"] == bubbles_before
        )


def test_scan_review_visibility_with_ratings_no_batch_counter() -> None:
    with Session(_engine()) as session:
        exam, students, form, comp_id = _seed_two_problem_ratings(session)
        from app.models import PeerReviewBatch

        for batch in session.scalars(
            select(PeerReviewBatch).where(PeerReviewBatch.form_id == form.id)
        ):
            batch.total_pages = 0
        session.commit()
        # Ratings still make the session visible in scan-review.
        assert _peer_review_has_scan_activity(session, exam) is True
