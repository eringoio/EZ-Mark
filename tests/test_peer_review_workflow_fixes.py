"""Tests for the rebuilt peer-review workflow: confirm/download generation, destructive
regeneration, multi-file scan upload, image+overlay review, and the results CTA path.
"""

import cv2
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base, SessionLocal, data_relative_path
from app.main import create_app
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewBatch,
    PeerReviewFinalResult,
    PeerReviewGroupResult,
    PeerReviewRating,
    PeerReviewSubmissionPage,
    Student,
)
from app.services.peer_review_builder_service import get_or_create_form
from app.services.peer_review_review_service import (
    apply_rating_correction,
    build_review_context,
)
from app.services.peer_review_scan_service import (
    _align_peer_review_page,
    _detect_qr_token,
    _read_page_scores,
    create_peer_review_batch,
    record_submission_page,
)
from app.services.peer_review_sheet_service import (
    generate_peer_review_pages,
    generate_peer_review_pdf,
    regenerate_peer_review_sheets,
)
from tests.pdf_helpers import full_text, render_page_to_bgr


def _peer_review_exam(session: Session) -> tuple[MockExam, list[Student]]:
    exam = MockExam(name="Presentation Review", assessment_type="peer_review")
    students = [
        Student(student_number="001", name="Student One", class_name="A"),
        Student(student_number="002", name="Student Two", class_name="A"),
    ]
    session.add_all([exam, *students])
    session.flush()
    for index, student in enumerate(students, start=1):
        session.add(ExamStudent(exam_id=exam.id, student_id=student.id, seat_order=index))
    session.add(Competency(exam_id=exam.id, name="Clarity", display_order=1))
    session.commit()
    return exam, students


def test_regenerate_clears_scans_results_and_mints_new_tokens() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        pages = generate_peer_review_pages(session, exam)
        old_tokens = {page.qr_token for page in pages}
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.pdf", "uploaded_scans/scan.pdf"
        )
        record_submission_page(
            session, batch, pages[0], 1, "processed_pages/a.png", {(students[0].id, comp_id): 4}
        )
        session.add(
            PeerReviewFinalResult(
                exam_id=exam.id, form_id=form.id, target_student_id=students[0].id, final_score=4.0
            )
        )
        session.add(
            PeerReviewGroupResult(
                exam_id=exam.id,
                form_id=form.id,
                rater_group_id=form.rater_groups[0].id,
                target_student_id=students[0].id,
                group_average_score=4.0,
                rating_count=1,
            )
        )
        session.commit()
        assert session.query(PeerReviewRating).count() > 0

        summary = regenerate_peer_review_sheets(session, exam)

        assert session.query(PeerReviewRating).count() == 0
        assert session.query(PeerReviewBatch).count() == 0
        assert session.query(PeerReviewSubmissionPage).count() == 0
        assert session.query(PeerReviewFinalResult).count() == 0
        assert session.query(PeerReviewGroupResult).count() == 0
        session.refresh(form)
        new_tokens = {page.qr_token for page in form.pages}
        assert new_tokens and new_tokens.isdisjoint(old_tokens)
        assert summary.pages_created == len(form.pages)
        # Sheets are re-locked after regeneration.
        assert exam.status == "printed"


def test_review_context_overlay_and_correction_resolves_queue() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam, students = _peer_review_exam(session)
        form = get_or_create_form(session, exam)
        page = generate_peer_review_pages(session, exam)[0]
        comp_id = session.scalar(select(Competency.id).where(Competency.exam_id == exam.id))
        batch = create_peer_review_batch(
            session, form.id, form.rater_groups[0].id, "scan.pdf", "uploaded_scans/scan.pdf"
        )
        # One unreadable (blank) rating becomes a review-queue item.
        record_submission_page(
            session,
            batch,
            page,
            1,
            "processed_pages/a.png",
            {(students[0].id, comp_id): None, (students[1].id, comp_id): 5},
        )

        context = build_review_context(session, form)
        assert context["problem_count"] >= 1
        assert context["current"] is not None
        assert context["image_url"].endswith("/image")
        assert context["coordinate_map"]["rating_bubbles"]
        key = f"{context['current'].target_student_id}_{context['current'].competency_id}"
        assert key in context["rating_map"]
        assert context["rating_map"][key]["is_current"] is True

        apply_rating_correction(
            session,
            context["current"],
            score=4,
            status="reviewed",
            score_min=form.score_min,
            score_max=form.score_max,
        )

        resolved = build_review_context(session, form)
        assert resolved["problem_count"] == 0
        corrected = session.get(PeerReviewRating, context["current"].id)
        assert corrected.final_score == 4
        assert corrected.status == "reviewed"
        assert corrected.reviewed_by == "staff"
        assert corrected.reviewed_at is not None
        # Submission/batch state advances once nothing on the page needs review.
        submission = session.get(PeerReviewSubmissionPage, corrected.submission_page_id)
        assert submission.review_status == "ok"


def test_real_generated_pdf_reads_blank_unmarked_and_ok_when_marked() -> None:
    """End-to-end against the actual rendered PDF (not synthetic bubbles).

    Validates the geometry + light-grey printing together: the QR and corner markers are
    detectable, alignment succeeds, an *unmarked* printed sheet reads as all blank (the
    printed outlines/digits/grid are never read as marks), and a real dark mark reads back
    as that score. This is the core fix for the "everything comes back blank/unclear"
    reliability problem.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        exam = MockExam(name="Real PR", assessment_type="peer_review")
        students = [
            Student(student_number=f"00{i}", name=f"Student {i}", class_name="A")
            for i in range(1, 6)
        ]
        session.add_all([exam, *students])
        session.flush()
        for index, student in enumerate(students, start=1):
            session.add(ExamStudent(exam_id=exam.id, student_id=student.id, seat_order=index))
        for order, name in enumerate(["Clarity", "Delivery", "Content", "Logic", "Q&A"], start=1):
            session.add(Competency(exam_id=exam.id, name=name, display_order=order))
        session.commit()

        page = generate_peer_review_pages(session, exam)[0]
        summary = generate_peer_review_pdf(session, exam)
        cmap = page.coordinate_map_json
        # 5 criteria + scale 1-5 must resolve to landscape and fit all five students.
        assert cmap["orientation"] == "landscape"
        assert len(cmap["target_rows"]) == 5

        pdf_path = get_settings().data_root / summary.pdf_path
        text = full_text(pdf_path)
        assert "印刷スケール" not in text
        assert "Print at 100% scale" not in text
        image = render_page_to_bgr(pdf_path, 0, dpi=300)
        out_dir = get_settings().data_root / "processed_pages"
        out_dir.mkdir(parents=True, exist_ok=True)
        blank_path = out_dir / "real_blank.png"
        cv2.imwrite(str(blank_path), image)
        blank_rel = data_relative_path(blank_path)

        assert _detect_qr_token(get_settings().data_root / blank_rel) == page.qr_token
        aligned_rel, status = _align_peer_review_page(page, blank_rel)
        assert status in {"ok", "partial"}

        _scores, _conf, statuses = _read_page_scores(page, aligned_rel or blank_rel, session)
        assert statuses, "expected rating cells from the coordinate map"
        # Nothing is marked, so nothing should read as a confident answer.
        assert all(value == "blank" for value in statuses.values())

        # Draw a real dark mark in one bubble on the aligned sheet and re-read it.
        aligned = cv2.imread(str(get_settings().data_root / (aligned_rel or blank_rel)))
        height, width = aligned.shape[:2]
        comp_id = cmap["rating_bubbles"][0]["competency_id"]
        bubble = next(
            b
            for b in cmap["rating_bubbles"]
            if b["target_student_id"] == students[0].id
            and b["competency_id"] == comp_id
            and b["score"] == 4
        )
        cx = int(bubble["x_mm"] / cmap["page_width_mm"] * width)
        cy = int(bubble["y_mm"] / cmap["page_height_mm"] * height)
        radius = int(bubble["radius_mm"] / cmap["page_width_mm"] * width)
        cv2.circle(aligned, (cx, cy), radius, (0, 0, 0), thickness=-1)
        marked_path = out_dir / "real_marked.png"
        cv2.imwrite(str(marked_path), aligned)

        marked_scores, _c, marked_statuses = _read_page_scores(
            page, data_relative_path(marked_path), session
        )
        key = (students[0].id, comp_id)
        assert marked_statuses[key] == "ok"
        assert marked_scores[key] == 4


# --- Route-level checks (confirm/download generation, recalculate status) ------


def _client() -> TestClient:
    return TestClient(create_app())


def _peer_run_with_student(client: TestClient) -> int:
    response = client.post(
        "/templates/new",
        data={"name": "Workflow PR", "assessment_type": "peer_review"},
        follow_redirects=False,
    )
    template_id = int(response.headers["location"].split("/")[2])
    run = client.post(
        f"/templates/{template_id}/runs/new",
        data={"run_name": "Class A", "exam_date": "2026-07-01"},
        follow_redirects=False,
    )
    run_id = int(run.headers["location"].split("/")[2])
    client.post(
        f"/exams/{run_id}/students/add-manual",
        data={"student_number": "2505ma04", "name": "aaa", "class_name": "3A"},
        follow_redirects=False,
    )
    return run_id


def test_generate_route_returns_json_and_download_for_fetch() -> None:
    with _client() as client:
        run_id = _peer_run_with_student(client)

        response = client.post(
            f"/exams/{run_id}/peer-review/sheets/generate",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["download"].endswith("/peer-review/sheets/download/latest")

        download = client.get(body["download"])
        assert download.status_code == 200
        assert download.content[:4] == b"%PDF"


def test_recalculate_route_marks_results_ready() -> None:
    with _client() as client:
        run_id = _peer_run_with_student(client)
        client.post(
            f"/exams/{run_id}/peer-review/sheets/generate", headers={"Accept": "application/json"}
        )

        response = client.post(
            f"/exams/{run_id}/peer-review/results/recalculate", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/exams/{run_id}/results"

        db = SessionLocal()
        try:
            exam = db.get(MockExam, run_id)
            assert exam.status == "results_ready"
        finally:
            db.close()
