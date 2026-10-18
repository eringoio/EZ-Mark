"""Follow-up regression tests for the peer-review workflow (final pass before classroom use).

Covers:
- reviewed_blank / reviewed_invalid resolve out of the problem queue, carry no score, are
  ignored by scoring, and survive recalculation (Task 1);
- submission/batch status flip to ok/processed once all problems on a page are resolved
  (Task 1);
- the ``ready_to_score`` next-action routes to the results page so its label and
  destination agree (Task 2);
- review-queue/current labels use the printed-sheet ``display_label`` and never leak the
  student code (Task 3);
- the review context stays robust to old ratings whose target was removed/disabled, while
  still excluding students from other exams (Task 4);
- the review page renders with missing target/student data, showing a safe fallback (Task 5);
- the results page only reports ``has_results`` for real scored data, not empty group
  sections (Task 6).
"""

from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base, SessionLocal
from app.main import create_app
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewRating,
    PeerReviewSubmissionPage,
    PeerReviewTarget,
    Student,
)
from app.routes import peer_review as peer_review_routes
from app.services.exam_service import next_exam_action
from app.services.i18n_service import _
from app.services.peer_review_builder_service import get_or_create_form
from app.services.peer_review_insights_service import build_peer_review_insights
from app.services.peer_review_review_service import (
    PROBLEM_STATUSES,
    apply_rating_correction,
    build_review_context,
    list_problem_ratings,
    resolve_remaining_submission_ratings,
)
from app.services.peer_review_scan_service import (
    create_peer_review_batch,
    record_submission_page,
)
from app.services.peer_review_scoring_service import (
    recalculate_anonymous_peer_review_results,
)
from app.services.peer_review_sheet_service import generate_peer_review_pages
from app.services.templates import templates


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


def _seed_two_problem_ratings(session: Session):
    """Two unreadable (blank) ratings on one submission page -> two review-queue items."""
    exam, students = _peer_review_exam(session)
    form = get_or_create_form(session, exam)
    page = generate_peer_review_pages(session, exam)[0]
    comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
    batch = create_peer_review_batch(
        session, form.id, form.rater_groups[0].id, "scan.pdf", "uploaded_scans/scan.pdf"
    )
    submission = record_submission_page(
        session,
        batch,
        page,
        1,
        "processed_pages/a.png",
        {(students[0].id, comp_id): None, (students[1].id, comp_id): None},
    )
    return exam, students, form, comp_id, batch, submission


# --- Task 1: reviewed_blank / reviewed_invalid -----------------------------------


def test_reviewed_blank_resolves_out_of_queue_with_no_score() -> None:
    with Session(_engine()) as session:
        _exam, _students, form, _comp_id, _batch, _sub = _seed_two_problem_ratings(session)
        problems = list_problem_ratings(session, form)
        apply_rating_correction(
            session, problems[0], score=None, status="blank",
            score_min=form.score_min, score_max=form.score_max,
        )
        rating = session.get(PeerReviewRating, problems[0].id)
        assert rating.status == "reviewed_blank"
        assert rating.final_score is None
        # reviewed_blank is a resolution, never an unresolved problem.
        assert "reviewed_blank" not in PROBLEM_STATUSES
        assert rating.id not in {r.id for r in list_problem_ratings(session, form)}


def test_reviewed_invalid_resolves_out_of_queue_with_no_score() -> None:
    with Session(_engine()) as session:
        _exam, _students, form, _comp_id, _batch, _sub = _seed_two_problem_ratings(session)
        problems = list_problem_ratings(session, form)
        apply_rating_correction(
            session, problems[0], score=None, status="invalid",
            score_min=form.score_min, score_max=form.score_max,
        )
        rating = session.get(PeerReviewRating, problems[0].id)
        assert rating.status == "reviewed_invalid"
        assert rating.final_score is None
        assert "reviewed_invalid" not in PROBLEM_STATUSES
        assert rating.id not in {r.id for r in list_problem_ratings(session, form)}


def test_resolving_last_problem_empties_queue_and_flips_statuses() -> None:
    with Session(_engine()) as session:
        _exam, _students, form, _comp_id, batch, submission = _seed_two_problem_ratings(session)
        # Mimic the post-scan state: the page and its batch need review.
        submission.review_status = "needs_review"
        batch.status = "needs_review"
        batch.problem_pages = 1
        session.commit()

        problems = list_problem_ratings(session, form)
        assert len(problems) == 2
        # First resolution (reviewed_blank): one problem still remains on the page.
        apply_rating_correction(
            session, problems[0], score=None, status="blank",
            score_min=form.score_min, score_max=form.score_max,
        )
        session.refresh(submission)
        session.refresh(batch)
        assert submission.review_status == "needs_review"
        assert batch.status == "needs_review"

        # Last resolution (reviewed_invalid): queue empties, submission/batch advance.
        apply_rating_correction(
            session, problems[1], score=None, status="invalid",
            score_min=form.score_min, score_max=form.score_max,
        )
        assert build_review_context(session, form)["problem_count"] == 0
        session.refresh(submission)
        session.refresh(batch)
        assert submission.review_status == "ok"
        assert batch.status == "processed"
        assert batch.problem_pages == 0


def test_recalculation_and_scoring_ignore_reviewed_blank_and_invalid() -> None:
    with Session(_engine()) as session:
        _exam, students, form, _comp_id, _batch, _sub = _seed_two_problem_ratings(session)
        problems = list_problem_ratings(session, form)
        apply_rating_correction(
            session, problems[0], score=None, status="blank",
            score_min=form.score_min, score_max=form.score_max,
        )
        apply_rating_correction(
            session, problems[1], score=None, status="invalid",
            score_min=form.score_min, score_max=form.score_max,
        )
        # Recalculation must not raise when only non-scoring resolutions exist...
        finals = recalculate_anonymous_peer_review_results(session, form)
        # ...and the non-scoring ratings contribute nothing to any final score.
        assert finals
        assert all(final.final_score is None for final in finals)
        from app.models import PeerReviewGroupResult

        group_results = session.scalars(
            select(PeerReviewGroupResult).where(PeerReviewGroupResult.form_id == form.id)
        )
        assert all(result.group_average_score is None for result in group_results)


def test_resolve_remaining_submission_ratings_marks_only_problem_statuses_blank() -> None:
    with Session(_engine()) as session:
        _exam, students, form, comp_id, batch, submission = _seed_two_problem_ratings(session)
        existing = list(session.scalars(select(PeerReviewRating).where(PeerReviewRating.form_id == form.id)))
        for rating in existing:
            session.delete(rating)
        session.flush()
        statuses = [
            "blank",
            "multiple_mark",
            "unclear",
            "invalid",
            "reviewed",
            "ok",
            "reviewed_blank",
            "reviewed_invalid",
        ]
        ratings = [
            PeerReviewRating(
                exam_id=form.exam_id,
                form_id=form.id,
                batch_id=batch.id,
                rater_group_id=batch.rater_group_id,
                submission_page_id=submission.id,
                peer_review_page_id=submission.peer_review_page_id,
                target_student_id=students[0].id,
                competency_id=comp_id,
                status=status,
            )
            for status in statuses
        ]
        other_submission = PeerReviewSubmissionPage(
            batch_id=batch.id,
            peer_review_page_id=submission.peer_review_page_id,
            page_no_in_upload=2,
            image_path="processed_pages/b.png",
            review_status="needs_review",
        )
        other_rating = PeerReviewRating(
            exam_id=form.exam_id,
            form_id=form.id,
            batch_id=batch.id,
            rater_group_id=batch.rater_group_id,
            submission_page_id=other_submission.id,
            peer_review_page_id=submission.peer_review_page_id,
            target_student_id=students[1].id,
            competency_id=comp_id,
            status="unclear",
        )
        session.add_all([*ratings, other_submission])
        session.flush()
        other_rating.submission_page_id = other_submission.id
        session.add(other_rating)
        submission.review_status = "needs_review"
        batch.status = "needs_review"
        batch.problem_pages = 2
        session.commit()

        changed = resolve_remaining_submission_ratings(session, submission.id, "blank")

        assert changed == 4
        assert [rating.status for rating in ratings[:4]] == ["reviewed_blank"] * 4
        assert all(rating.final_score is None for rating in ratings[:4])
        assert [rating.status for rating in ratings[4:]] == [
            "reviewed",
            "ok",
            "reviewed_blank",
            "reviewed_invalid",
        ]
        assert other_rating.status == "unclear"
        session.refresh(submission)
        session.refresh(batch)
        assert submission.review_status == "ok"
        assert batch.status == "needs_review"
        assert batch.problem_pages == 1


def test_resolve_remaining_submission_ratings_marks_only_problem_statuses_invalid() -> None:
    with Session(_engine()) as session:
        _exam, _students, form, _comp_id, batch, submission = _seed_two_problem_ratings(session)
        submission.review_status = "needs_review"
        batch.status = "needs_review"
        batch.problem_pages = 1
        session.commit()

        changed = resolve_remaining_submission_ratings(session, submission.id, "invalid")

        assert changed == 2
        statuses = [
            rating.status
            for rating in session.scalars(
                select(PeerReviewRating).where(PeerReviewRating.submission_page_id == submission.id)
            )
        ]
        assert statuses == ["reviewed_invalid", "reviewed_invalid"]
        session.refresh(submission)
        session.refresh(batch)
        assert submission.review_status == "ok"
        assert batch.status == "processed"


def test_peer_review_bulk_route_redirects_and_applies_change() -> None:
    with Session(_engine()) as session:
        exam, _students, _form, _comp_id, _batch, submission = _seed_two_problem_ratings(session)

        redirect = peer_review_routes.mark_remaining_peer_review_page_blank(
            exam.id, submission.id, db=session
        )

        assert redirect.status_code == 303
        assert redirect.headers["location"] == f"/exams/{exam.id}/peer-review/review"
        statuses = [
            rating.status
            for rating in session.scalars(
                select(PeerReviewRating).where(PeerReviewRating.submission_page_id == submission.id)
            )
        ]
        assert statuses == ["reviewed_blank", "reviewed_blank"]


def test_peer_review_template_includes_bulk_page_actions() -> None:
    with Session(_engine()) as session:
        exam, _students, form, _comp_id, _batch, submission = _seed_two_problem_ratings(session)
        context = build_review_context(session, form)
        html = templates.env.get_template("peer_review/review.html").render(
            request=SimpleNamespace(url_for=lambda _name, **kwargs: kwargs.get("path", "")),
            current_locale="en",
            supported_locales={"en": "English"},
            exam=exam,
            form=form,
            progress={},
            **context,
            _=lambda key, **kwargs: (
                "Mark remaining {count} on this page as invalid".format(**kwargs)
                if key == "review.bulk_page.remaining_invalid"
                else key
            ),
        )

        assert f"/submission-pages/{submission.id}/mark-remaining-invalid" in html
        assert "Mark remaining 2 on this page as invalid" in html


# --- Task 2: ready_to_score next action ------------------------------------------


def test_ready_to_score_action_label_and_url_agree() -> None:
    exam = MockExam(id=1, name="PR", assessment_type="peer_review", status="ready_to_score")
    label, url = next_exam_action(exam)
    # The label is the "calculate results" CTA, which the results page fulfils with its
    # recalculate button — so label and destination now match.
    assert label == _("exams.action.calculate_results")
    assert url == "/exams/1/results"
    # A fully-resolved session is never sent back into the problem review queue.
    assert "/peer-review/review" not in url


# --- Tasks 3 & 4: review-context labels and robustness ---------------------------


def test_review_queue_labels_use_target_display_label() -> None:
    with Session(_engine()) as session:
        _exam, _students, form, _comp_id, _batch, _sub = _seed_two_problem_ratings(session)
        context = build_review_context(session, form)
        labels = {item["target"] for item in context["queue"]}
        # Printed-sheet labels ("<order>. <name>"), never the student code.
        assert labels == {"1. Yamada Taro", "2. Tanaka Hanako"}
        for label in labels:
            assert "001" not in label
            assert "002" not in label
        # The current item uses the same label vocabulary as the queue/printed sheet.
        assert context["current_target_label"] in labels


def test_review_context_robust_to_removed_target() -> None:
    with Session(_engine()) as session:
        _exam, students, form, _comp_id, _batch, _sub = _seed_two_problem_ratings(session)
        # Unrelated student in another exam must never appear here.
        other_exam = MockExam(name="Other", assessment_type="peer_review")
        outsider = Student(student_number="999", name="Outsider", class_name="9Z")
        session.add_all([other_exam, outsider])
        # Remove one target after scanning; its rating still references the student.
        removed = session.scalar(
            select(PeerReviewTarget)
            .where(PeerReviewTarget.form_id == form.id)
            .where(PeerReviewTarget.student_id == students[1].id)
        )
        session.delete(removed)
        session.commit()

        context = build_review_context(session, form)
        labels = {item["target"] for item in context["queue"]}
        # The removed target's rating still resolves to the real student name (fallback),
        # not "—" or a crash.
        assert any("Tanaka Hanako" in label for label in labels)
        # ...and students from other exams are still excluded.
        assert all("Outsider" not in label for label in labels)


# --- Task 5: null-safe review page rendering -------------------------------------


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_run_with_pages(client: TestClient) -> int:
    tpl = client.post(
        "/templates/new",
        data={"name": "PR Followup", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    template_id = int(tpl.headers["location"].split("/")[2])
    run = client.post(
        f"/templates/{template_id}/runs/new",
        data={"run_name": "Class A", "exam_date": "2026-07-01"},
        follow_redirects=False,
    )
    run_id = int(run.headers["location"].split("/")[2])
    for number, name in (("S1", "Alice"), ("S2", "Bob")):
        client.post(
            f"/exams/{run_id}/students/add-manual",
            data={"student_number": number, "name": name, "class_name": "3A"},
            follow_redirects=False,
        )
    # Generate sheets so the form, pages, targets, and default rater group exist.
    client.post(
        f"/exams/{run_id}/peer-review/sheets/generate", headers={"Accept": "application/json"}
    )
    return run_id


def test_review_page_renders_with_missing_target_student() -> None:
    with _client() as client:
        run_id = _peer_run_with_pages(client)
        db = SessionLocal()
        try:
            exam = db.get(MockExam, run_id)
            form = get_or_create_form(db, exam)
            comp_id = db.scalar(select(Competency.id).where(Competency.exam_id == run_id))
            # A problem rating whose target student is no longer present at all.
            db.add(
                PeerReviewRating(
                    exam_id=run_id,
                    form_id=form.id,
                    batch_id=0,
                    rater_group_id=form.rater_groups[0].id,
                    submission_page_id=0,
                    peer_review_page_id=form.pages[0].id,
                    target_student_id=987654,
                    competency_id=comp_id,
                    detected_score=None,
                    final_score=None,
                    status="blank",
                )
            )
            db.commit()
        finally:
            db.close()

        response = client.get(f"/exams/{run_id}/peer-review/review")
        assert response.status_code == 200
        # Safe fallback shown instead of a crash.
        assert "—" in response.text


# --- Task 6: results-page has_results detection ----------------------------------


def test_insights_has_results_false_for_empty_group_sections() -> None:
    with Session(_engine()) as session:
        exam, _students, form, _comp_id, _batch, _sub = _seed_two_problem_ratings(session)
        for rating in list_problem_ratings(session, form):
            apply_rating_correction(
                session, rating, score=None, status="blank",
                score_min=form.score_min, score_max=form.score_max,
            )
        recalculate_anonymous_peer_review_results(session, form)
        insights = build_peer_review_insights(session, exam)
        # Group sections exist (one per rater group) but every row is empty.
        assert insights["group_rankings"]
        assert all(not section["rows"] for section in insights["group_rankings"])
        assert insights["has_results"] is False


def test_insights_has_results_true_with_real_scores() -> None:
    with Session(_engine()) as session:
        exam, _students, form, _comp_id, _batch, _sub = _seed_two_problem_ratings(session)
        for rating in list_problem_ratings(session, form):
            apply_rating_correction(
                session, rating, score=4, status="reviewed",
                score_min=form.score_min, score_max=form.score_max,
            )
        recalculate_anonymous_peer_review_results(session, form)
        insights = build_peer_review_insights(session, exam)
        assert insights["has_results"] is True


def test_insights_empty_does_not_crash_and_has_no_results() -> None:
    with Session(_engine()) as session:
        exam, _students = _peer_review_exam(session)
        # No form/results yet -> empty insights, all sections empty, no crash.
        insights = build_peer_review_insights(session, exam)
        assert insights["has_results"] is False
        assert insights["combined_ranking"] == []
        assert insights["side_by_side"] == []
        assert insights["group_rankings"] == []
        assert insights["competency_rankings"] == []


def test_results_page_empty_state_when_group_sections_have_no_rows() -> None:
    with _client() as client:
        run_id = _peer_run_with_pages(client)
        db = SessionLocal()
        try:
            exam = db.get(MockExam, run_id)
            form = get_or_create_form(db, exam)
            comp_id = db.scalar(select(Competency.id).where(Competency.exam_id == run_id))
            group_id = form.rater_groups[0].id
            # Only confirmed-blank (non-scoring) ratings -> recalc makes empty sections.
            for target in form.targets:
                db.add(
                    PeerReviewRating(
                        exam_id=run_id,
                        form_id=form.id,
                        batch_id=0,
                        rater_group_id=group_id,
                        submission_page_id=0,
                        peer_review_page_id=form.pages[0].id,
                        target_student_id=target.student_id,
                        competency_id=comp_id,
                        detected_score=None,
                        final_score=None,
                        status="reviewed_blank",
                    )
                )
            db.commit()
            recalculate_anonymous_peer_review_results(db, form)
        finally:
            db.close()

        response = client.get(f"/exams/{run_id}/results")
        assert response.status_code == 200
        assert 'class="empty-state"' in response.text
        assert 'class="metric-grid"' not in response.text


def test_results_page_shows_data_with_real_scored_rows() -> None:
    with _client() as client:
        run_id = _peer_run_with_pages(client)
        db = SessionLocal()
        try:
            exam = db.get(MockExam, run_id)
            form = get_or_create_form(db, exam)
            comp_id = db.scalar(select(Competency.id).where(Competency.exam_id == run_id))
            group_id = form.rater_groups[0].id
            for target in form.targets:
                db.add(
                    PeerReviewRating(
                        exam_id=run_id,
                        form_id=form.id,
                        batch_id=0,
                        rater_group_id=group_id,
                        submission_page_id=0,
                        peer_review_page_id=form.pages[0].id,
                        target_student_id=target.student_id,
                        competency_id=comp_id,
                        detected_score=4,
                        final_score=4,
                        status="ok",
                    )
                )
            db.commit()
            recalculate_anonymous_peer_review_results(db, form)
        finally:
            db.close()

        response = client.get(f"/exams/{run_id}/results")
        assert response.status_code == 200
        assert 'class="metric-grid"' in response.text
