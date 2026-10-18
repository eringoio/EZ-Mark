"""Prompt 10 gap coverage: Standard Test route-level scan pipeline.

Covers the happy path end-to-end against a *real* generated marksheet PDF:

1. uploading the multi-page PDF through ``POST /exams/{id}/scan-batches/upload``
   splits it into one ``ScannedPage`` per student sheet and QR-matches every
   page to the printed ``SheetInstance``;
2. a genuinely hand-marked bubble is read back by the OMR engine and there the
   raw Standard Test score follows from the answer key.
"""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.database as app_database
import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base, SessionLocal
from app.main import create_app
from app.models import (
    ExamStudent,
    MockExam,
    PhysicalSheet,
    Question,
    ScanBatch,
    ScoringSection,
    Student,
)
from app.services.scan_batch_service import create_scan_batch, process_scan_batch
from app.services.scoring_service import recalculate_exam_results
from app.services.sheet_generation_service import generate_exam_sheets
from tests.pdf_helpers import render_page_to_bgr


def _quiz(db: Session, *, student_count: int = 3) -> MockExam:
    """A small Standard Test run (one landscape sheet per student)."""
    exam = MockExam(name="Scan E2E Run", assessment_type="generic_test")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="Vocabulary", slug="vocab", display_order=1)
    section = ScoringSection(code="lk", name="Language", display_order=1)
    exam.physical_sheets.append(sheet)
    exam.scoring_sections.append(section)
    students = [
        Student(student_number=f"S{i:03d}", name=f"Student {i}", class_name="A")
        for i in range(1, student_count + 1)
    ]
    db.add(exam)
    db.add_all(students)
    db.commit()
    for order, student in enumerate(students, start=1):
        db.add(ExamStudent(exam_id=exam.id, student_id=student.id, seat_order=order))
    for index in range(1, 5):
        db.add(
            Question(
                exam_id=exam.id,
                physical_sheet_id=sheet.id,
                scoring_section_id=section.id,
                question_no=index,
                display_label=str(index),
                correct_answer=str(index),
                choice_count=4,
                points=1,
                display_order=index,
            )
        )
    db.commit()
    return exam


@pytest.fixture()
def initialized_db():
    app_database.init_database()


def test_upload_route_splits_and_qr_matches_every_sheet(initialized_db) -> None:
    with SessionLocal() as db:
        exam = _quiz(db)
        summary = generate_exam_sheets(db, exam)
        assert exam.sheet_instances, "expected one printed sheet per student"

        pdf_path = get_settings().data_root / summary.download_path
        content = pdf_path.read_bytes()
        instance_tokens = [instance.qr_token for instance in exam.sheet_instances]
        exam_id = exam.id

    client = TestClient(create_app())
    response = client.post(
        f"/exams/{exam_id}/scan-batches/upload",
        files=[("files", ("marks.pdf", content, "application/pdf"))],
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    assert response.headers["location"].startswith(f"/exams/{exam_id}/scan-batches")

    with SessionLocal() as db:
        batch = db.scalar(select(ScanBatch).order_by(ScanBatch.id.desc()))
        assert batch is not None
        assert batch.total_pages == 3  # one physical sheet per student
        pages = sorted(batch.scanned_pages, key=lambda page: page.page_no)
        assert len(pages) == 3

        matched_tokens = {page.sheet_instance.qr_token for page in pages}
        assert matched_tokens == set(instance_tokens)
        assert all(page.sheet_instance_id is not None for page in pages)
        # Every page carries responses for all four questions of the template.
        for page in pages:
            assert len(page.responses) == 4
            assert all(
                response.detected_answer is None and response.status == "blank"
                for response in page.responses
            )
        # No page was misrouted into the review queue by the reader.
        assert batch.status == "processed"


def _mark(image: np.ndarray, cmap: dict, question: dict, choice: str) -> np.ndarray:
    height, width = image.shape[:2]
    bubble = next(b for b in question["choices"] if b["choice"] == choice)
    cx = int(bubble["x_mm"] / cmap["page_width_mm"] * width)
    cy = int(bubble["y_mm"] / cmap["page_height_mm"] * height)
    radius = max(4, int(bubble["radius_mm"] / cmap["page_width_mm"] * width))
    return cv2.circle(image, (cx, cy), radius, (0, 0, 0), thickness=-1)


def test_hand_marked_bubbles_read_and_score() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        exam = _quiz(db, student_count=1)
        summary = generate_exam_sheets(db, exam)

        template = exam.sheet_instances[0].sheet_template
        cmap = template.coordinate_map_json
        pdf_path = get_settings().data_root / summary.download_path
        image = render_page_to_bgr(pdf_path, 0, dpi=300)

        # q1 -> choice 1 (correct), q2 -> choice 2 (correct), q3 blank, q4 -> choice 1 (wrong).
        by_no = {question["display_label"]: question for question in cmap["questions"]}
        _mark(image, cmap, by_no["1"], "1")
        _mark(image, cmap, by_no["2"], "2")
        _mark(image, cmap, by_no["4"], "1")

        processed_dir = get_settings().data_root / "processed_pages" / "e2e"
        processed_dir.mkdir(parents=True, exist_ok=True)
        scan_path = processed_dir / "marked_page.png"
        cv2.imwrite(str(scan_path), image)

        batch = create_scan_batch(db, exam.id, "marked_page.png", scan_path.read_bytes())
        summary_scan = process_scan_batch(db, batch)
        assert summary_scan.detected_pages == 1

        page = exam.sheet_instances[0].scanned_pages[-1]
        assert page.sheet_instance_id == exam.sheet_instances[0].id
        detected = {response.question.question_no: response for response in page.responses}
        assert detected[1].detected_answer == "1"
        assert detected[2].detected_answer == "2"
        assert detected[3].status == "blank"
        assert detected[4].detected_answer == "1"

        results = recalculate_exam_results(db, exam)
        assert len(results) == 1
        details = results[0].score_details_json
        assert details["total"] == 2.0
        assert details["correct_points"] == 2.0
        assert details["max_points"] == 4.0
        assert details["percentage"] == 50.0
