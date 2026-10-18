"""Session archive lifecycle: scan purge, read-only guards, hidden-by-default listings."""

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal, init_database
from app.main import create_app
from app.models import (
    Competency,
    ExamStudent,
    MockExam,
    PeerReviewBatch,
    PeerReviewForm,
    PeerReviewPage,
    PeerReviewRaterGroup,
    PeerReviewRating,
    PeerReviewSubmissionPage,
    PhysicalSheet,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    ScoringSection,
    Student,
    StudentResult,
)
from app.services.archive_service import PURGED_SCAN_PATH, archive_exam


def _write_data_file(relative: str, content: bytes = b"scan-bytes") -> None:
    path = get_settings().data_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _scanned_run(db, name: str, *, with_files: bool = True) -> int:
    """Create a normal run with one student, scan batch + page, and a result."""
    exam = MockExam(
        name=name,
        assessment_type="generic_test",
        status="results_ready",
        is_template=False,
    )
    student = Student(student_number="SYN-A", name="Archive Student", class_name="A")
    db.add_all([exam, student])
    db.commit()
    db.add(ExamStudent(exam_id=exam.id, student_id=student.id))
    batch = ScanBatch(
        exam_id=exam.id,
        uploaded_file_name="scan.pdf",
        uploaded_file_path=f"uploaded_scans/{name}/scan.pdf",
        total_pages=1,
    )
    db.add(batch)
    db.commit()
    page = ScannedPage(
        scan_batch_id=batch.id,
        page_no=1,
        image_path=f"processed_pages/{name}/page1.png",
        aligned_image_path=f"processed_pages/{name}/page1_aligned.png",
    )
    result = StudentResult(exam_id=exam.id, student_id=student.id, total_score=120)
    db.add_all([page, result])
    db.commit()
    if with_files:
        _write_data_file(batch.uploaded_file_path)
        _write_data_file(page.image_path)
        _write_data_file(page.aligned_image_path)
    return exam.id


def _run_with_review_response(db, name: str) -> tuple[int, int]:
    exam_id = _scanned_run(db, name, with_files=False)
    exam = db.get(MockExam, exam_id)
    sheet = PhysicalSheet(sheet_no=1, sheet_name="S", slug="s", display_order=1, exam_id=exam_id)
    section = ScoringSection(exam_id=exam_id, code="raw", name="Raw")
    db.add_all([sheet, section])
    db.commit()
    question = Question(
        exam_id=exam_id,
        physical_sheet_id=sheet.id,
        scoring_section_id=section.id,
        question_no=1,
        display_label="Q1",
        choice_count=4,
        correct_answer="2",
    )
    db.add(question)
    db.commit()
    page = exam.scan_batches[0].scanned_pages[0]
    response = Response(
        scanned_page_id=page.id,
        question_id=question.id,
        detected_answer="1",
        final_answer="1",
        status="needs_review",
    )
    db.add(response)
    db.commit()
    return exam_id, response.id


def _peer_run_with_rating(db, name: str) -> tuple[int, int]:
    exam = MockExam(name=name, assessment_type="peer_review", status="needs_review")
    student = Student(student_number="PR-A", name="Peer Student", class_name="A")
    db.add_all([exam, student])
    db.commit()
    form = PeerReviewForm(exam_id=exam.id, title=name, score_min=1, score_max=5)
    competency = Competency(exam_id=exam.id, name="Overall", min_score=1, max_score=5)
    db.add_all([form, competency])
    db.commit()
    group = PeerReviewRaterGroup(form_id=form.id, name="Students", weight_percent=100)
    page = PeerReviewPage(form_id=form.id, page_no=1, title="Page", qr_token=f"{name}-qr")
    db.add_all([group, page])
    db.commit()
    batch = PeerReviewBatch(
        form_id=form.id,
        rater_group_id=group.id,
        uploaded_file_name="peer.pdf",
        uploaded_file_path=PURGED_SCAN_PATH,
    )
    db.add(batch)
    db.commit()
    submission = PeerReviewSubmissionPage(
        batch_id=batch.id,
        peer_review_page_id=page.id,
        page_no_in_upload=1,
        image_path=PURGED_SCAN_PATH,
    )
    db.add(submission)
    db.commit()
    rating = PeerReviewRating(
        exam_id=exam.id,
        form_id=form.id,
        batch_id=batch.id,
        rater_group_id=group.id,
        submission_page_id=submission.id,
        peer_review_page_id=page.id,
        target_student_id=student.id,
        competency_id=competency.id,
        detected_score=2,
        final_score=2,
        status="needs_review",
    )
    db.add(rating)
    db.commit()
    return exam.id, rating.id


def test_archive_purges_scan_files_keeps_results() -> None:
    init_database()
    settings = get_settings()
    with SessionLocal() as db:
        exam_id = _scanned_run(db, "ArchivePurge")
        exam = db.get(MockExam, exam_id)
        batch = exam.scan_batches[0]
        page = batch.scanned_pages[0]
        uploaded = settings.data_root / batch.uploaded_file_path
        image = settings.data_root / page.image_path
        aligned = settings.data_root / page.aligned_image_path
        assert uploaded.exists() and image.exists() and aligned.exists()

        archive_exam(db, exam)

        # Files gone from disk.
        assert not uploaded.exists()
        assert not image.exists()
        assert not aligned.exists()
        # Path columns neutralised; non-null columns get the sentinel, nullable gets None.
        db.refresh(batch)
        db.refresh(page)
        assert batch.uploaded_file_path == PURGED_SCAN_PATH
        assert page.image_path == PURGED_SCAN_PATH
        assert page.aligned_image_path is None
        # Archive bookkeeping + retained result/insight rows.
        assert exam.status == "archived"
        assert exam.archived_previous_status == "results_ready"
        assert exam.archived_at is not None
        assert exam.scan_files_purged_at is not None
        assert len(exam.results) == 1


def test_archived_session_hidden_by_default_everywhere() -> None:
    init_database()
    with SessionLocal() as db:
        exam_id = _scanned_run(db, "ArchiveHidden")
        archive_exam(db, db.get(MockExam, exam_id))

    with TestClient(create_app()) as client:
        assert "ArchiveHidden" not in client.get("/exams").text
        assert "ArchiveHidden" not in client.get("/scan-review").text
        assert "ArchiveHidden" not in client.get("/insights").text
        # Visible again when explicitly requested.
        assert "ArchiveHidden" in client.get("/exams?archived=show").text
        assert "ArchiveHidden" in client.get("/exams?archived=only").text
        assert "ArchiveHidden" in client.get("/scan-review?archived=only").text
        assert "ArchiveHidden" in client.get("/insights?archived=only").text


def test_archived_session_mutations_blocked_views_allowed() -> None:
    init_database()
    with SessionLocal() as db:
        exam_id = _scanned_run(db, "ArchiveGuards")
        archive_exam(db, db.get(MockExam, exam_id))

    with TestClient(create_app()) as client:
        # Blocked (409).
        assert client.get(f"/exams/{exam_id}/edit").status_code == 409
        assert client.get(f"/exams/{exam_id}/students/import").status_code == 409
        assert client.post(f"/exams/{exam_id}/sheets/generate").status_code == 409
        upload = client.post(
            f"/exams/{exam_id}/scan-batches/upload",
            files={"files": ("s.pdf", b"x", "application/pdf")},
        )
        assert upload.status_code == 409
        assert client.get(f"/exams/{exam_id}/review").status_code == 409
        assert client.post(f"/exams/{exam_id}/results/recalculate").status_code == 409
        # Allowed.
        assert client.get(f"/exams/{exam_id}").status_code == 200
        assert client.get(f"/exams/{exam_id}/results").status_code == 200
        assert client.get(f"/exams/{exam_id}/portable-export").status_code == 200
        assert client.post(f"/exams/{exam_id}/delete", follow_redirects=False).status_code == 303


def test_archive_route_purges_and_keeps_results() -> None:
    init_database()
    with SessionLocal() as db:
        exam_id = _scanned_run(db, "ArchiveViaRoute")

    with TestClient(create_app()) as client:
        response = client.post(f"/exams/{exam_id}/archive", follow_redirects=False)
        assert response.status_code == 303
        assert "archived=only" in response.headers["location"]

    with SessionLocal() as db:
        exam = db.get(MockExam, exam_id)
        assert exam.status == "archived"
        assert exam.scan_batches[0].uploaded_file_path == PURGED_SCAN_PATH
        assert len(exam.results) == 1


def test_archived_scan_image_route_refuses_purged_file() -> None:
    init_database()
    with SessionLocal() as db:
        exam_id = _scanned_run(db, "ArchiveImage")
        archive_exam(db, db.get(MockExam, exam_id))
        page_id = db.get(MockExam, exam_id).scan_batches[0].scanned_pages[0].id

    with TestClient(create_app()) as client:
        assert client.get(f"/scanned-pages/{page_id}/image").status_code == 410


def test_archived_direct_review_posts_do_not_mutate() -> None:
    init_database()
    with SessionLocal() as db:
        exam_id, response_id = _run_with_review_response(db, "ArchiveDirectReview")
        archive_exam(db, db.get(MockExam, exam_id))

    with TestClient(create_app()) as client:
        assert client.post(f"/responses/{response_id}/correct", data={"final_answer": "2"}).status_code == 409
        assert client.post(f"/responses/{response_id}/mark-blank").status_code == 409
        assert client.post(f"/responses/{response_id}/mark-invalid").status_code == 409

    with SessionLocal() as db:
        response = db.get(Response, response_id)
        assert response.final_answer == "1"
        assert response.status == "needs_review"


def test_archived_direct_peer_review_posts_do_not_mutate() -> None:
    init_database()
    with SessionLocal() as db:
        exam_id, rating_id = _peer_run_with_rating(db, "ArchiveDirectPeer")
        archive_exam(db, db.get(MockExam, exam_id))

    with TestClient(create_app()) as client:
        # The archived guard must fire before any rating mutation.
        assert client.post(f"/exams/{exam_id}/peer-review/review/{rating_id}", data={"score": "5"}).status_code == 409
        assert client.post(f"/exams/{exam_id}/peer-review/submission-pages/{rating_id}/mark-remaining-blank").status_code == 409
        assert client.post(f"/exams/{exam_id}/peer-review/submission-pages/{rating_id}/mark-remaining-invalid").status_code == 409

    with SessionLocal() as db:
        rating = db.get(PeerReviewRating, rating_id)
        assert rating.final_score == 2
        assert rating.status == "needs_review"
