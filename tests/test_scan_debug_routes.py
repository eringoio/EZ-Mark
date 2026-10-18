from types import SimpleNamespace
from uuid import uuid4

import cv2
import numpy as np
import pytest

import app.models  # noqa: F401
from app.config import get_settings
from app.database import SessionLocal, init_database
from app.models import (
    ExamStudent,
    MockExam,
    PhysicalSheet,
    ScanBatch,
    ScannedPage,
    SheetInstance,
    SheetTemplate,
    Student,
)
from app.routes import scans as scan_routes


def test_scan_debug_endpoint_returns_404_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SCAN_DEBUG", "0")

    with SessionLocal() as db:
        with pytest.raises(scan_routes.HTTPException) as exc:
            scan_routes.latest_scan_pages_debug(_local_request(), db=db)
    assert exc.value.status_code == 404


def test_scan_debug_endpoint_returns_page_json_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SCAN_DEBUG", "1")
    init_database()
    page_id = _create_debug_page()

    with SessionLocal() as db:
        latest = scan_routes.latest_scan_pages_debug(_local_request(), db=db)
        payload = scan_routes.scanned_page_debug(page_id, _local_request(), db=db)

    assert "pages" in latest
    assert payload["scanned_page_id"] == page_id
    assert payload["opencv_original_image_readable"] is True
    assert payload["questions_count"] == 0
    assert payload["diagnosis"] == "Sheet template has no readable OMR coordinates."


def test_scan_debug_diagnoses_missing_aligned_output(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SCAN_DEBUG", "1")
    init_database()
    page_id = _create_debug_page(
        coordinate_map={
            "questions": [
                {
                    "question_id": 1,
                    "choices": [
                        {"choice": "1", "x_mm": 10, "y_mm": 10, "radius_mm": 2},
                    ],
                }
            ]
        },
        aligned_image_path="processed_pages/missing_aligned.png",
        alignment_status="ok",
    )

    with SessionLocal() as db:
        payload = scan_routes.scanned_page_debug(page_id, _local_request(), db=db)

    assert payload["aligned_image_exists"] is False
    assert payload["original_fallback_worked"] is False
    assert payload["diagnosis"] == (
        "Alignment output file is missing. OpenCV image writing may have failed. "
        "Aligned image is missing, but original image is readable. "
        "Response reading should fall back to the original image."
    )


def test_qr_debug_reports_exact_and_normalized_token_matches(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SCAN_DEBUG", "1")
    init_database()
    page_id = _create_debug_page()
    with SessionLocal() as db:
        page = db.get(ScannedPage, page_id)
        assert page is not None
        page.qr_status = "invalid"
        page.qr_token = f"  {page.sheet_instance.qr_token}  "
        db.commit()

    with SessionLocal() as db:
        payload = scan_routes.qr_debug_scanned_page(page_id, _local_request(), db=db)

    assert payload["decoded_token_length"] > payload["normalized_token_length"]
    assert payload["exact_sheet_instance_match_current_exam"] is False
    assert payload["normalized_sheet_instance_match_current_exam"] is True
    assert payload["decoded_token_equals_current_sheet_instance_token"] is False


def _create_debug_page(
    coordinate_map: dict | None = None,
    aligned_image_path: str | None = None,
    alignment_status: str = "failed",
) -> int:
    suffix = uuid4().hex
    image_path = get_settings().data_root / "processed_pages" / f"debug_page_{suffix}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(image_path), np.full((20, 20, 3), 255, dtype=np.uint8))
    with SessionLocal() as db:
        exam = MockExam(name="Debug Run", assessment_type="generic")
        student = Student(student_number=f"DBG{suffix[:8]}", name="Debug Student", class_name="A")
        sheet = PhysicalSheet(
            sheet_no=1,
            sheet_name="Debug Sheet",
            slug=f"debug-{suffix[:8]}",
            display_order=1,
        )
        exam.physical_sheets.append(sheet)
        db.add_all([exam, student])
        db.flush()
        db.add(ExamStudent(exam_id=exam.id, student_id=student.id))
        template = SheetTemplate(
            exam_id=exam.id,
            physical_sheet_id=sheet.id,
            coordinate_map_json=coordinate_map or {},
        )
        db.add(template)
        db.flush()
        instance = SheetInstance(
            exam_id=exam.id,
            student_id=student.id,
            physical_sheet_id=sheet.id,
            sheet_template_id=template.id,
            qr_token=f"debug-token-{exam.id}",
            printed_name=student.name,
            printed_student_number=student.student_number,
        )
        batch = ScanBatch(
            exam_id=exam.id,
            uploaded_file_name="debug.pdf",
            uploaded_file_path="uploaded_scans/debug.pdf",
            total_pages=1,
        )
        page = ScannedPage(
            scan_batch=batch,
            sheet_instance=instance,
            student_id=student.id,
            physical_sheet_id=sheet.id,
            page_no=1,
            image_path=f"processed_pages/debug_page_{suffix}.png",
            aligned_image_path=aligned_image_path,
            qr_status="manual",
            alignment_status=alignment_status,
            review_status="needs_review",
        )
        db.add_all([instance, batch, page])
        db.commit()
        return page.id


def _local_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"host": "127.0.0.1:8000"},
    )
