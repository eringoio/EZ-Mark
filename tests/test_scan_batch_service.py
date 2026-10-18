from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.config import get_settings
from app.database import Base
from app.models import (
    ExamStudent,
    MockExam,
    PhysicalSheet,
    Question,
    Response,
    ScanBatch,
    ScannedPage,
    SheetInstance,
    SheetTemplate,
    Student,
)
from app.services import image_loading_service, scan_batch_service
from app.services.scan_batch_service import (
    batch_has_protected_work,
    normalize_page_review_status,
    process_scan_batch,
    refresh_page_responses_for_status,
    reprocess_unmatched_pages,
)


def test_detect_qr_token_uses_cropped_fallback(monkeypatch, tmp_path) -> None:
    image = np.full((100, 100, 3), 255, dtype=np.uint8)
    path = tmp_path / "scan.jpg"
    path.write_bytes(b"placeholder")

    class FakeDetector:
        def detectAndDecode(self, candidate):
            # First crop spec (0.62, 0.00, 1.00, 0.38) on a 100x100 image.
            if candidate.shape[:2] == (38, 38):
                return "opaque-token", None, None
            return "", None, None

    monkeypatch.setattr(scan_batch_service.cv2, "imread", lambda *_args: image)
    monkeypatch.setattr(scan_batch_service.cv2, "QRCodeDetector", FakeDetector)

    assert scan_batch_service._detect_qr_token(path) == "opaque-token"


def test_qr_detection_candidates_include_scaled_full_image() -> None:
    image = np.full((120, 200, 3), 255, dtype=np.uint8)

    shapes = [
        candidate.shape[:2] for candidate in scan_batch_service._qr_detection_candidates(image)
    ]

    assert (240, 400) in shapes


def _scan_fixture(session: Session) -> dict:
    """An exam with one matched+reviewed page and one unmatched page."""
    exam = MockExam(name="Sample Mock", assessment_type="generic_test")
    student_a = Student(student_number="SYN001", name="Student A", class_name="A")
    student_b = Student(student_number="SYN002", name="Student B", class_name="A")
    sheet = PhysicalSheet(sheet_no=1, sheet_name="Sheet", slug="sheet", display_order=1)
    exam.physical_sheets.append(sheet)
    session.add_all([exam, student_a, student_b])
    session.commit()
    session.add_all(
        [
            ExamStudent(exam_id=exam.id, student_id=student_a.id),
            ExamStudent(exam_id=exam.id, student_id=student_b.id),
        ]
    )
    question = Question(
        exam_id=exam.id,
        physical_sheet_id=sheet.id,
        display_label="1",
        choice_count=4,
        correct_answer="1",
    )
    template = SheetTemplate(exam_id=exam.id, physical_sheet_id=sheet.id)
    session.add_all([question, template])
    session.flush()
    instance_a = SheetInstance(
        exam_id=exam.id,
        student_id=student_a.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="token-a",
        printed_name=student_a.name,
        printed_student_number=student_a.student_number,
    )
    instance_b = SheetInstance(
        exam_id=exam.id,
        student_id=student_b.id,
        physical_sheet_id=sheet.id,
        sheet_template_id=template.id,
        qr_token="token-b",
        printed_name=student_b.name,
        printed_student_number=student_b.student_number,
    )
    batch = ScanBatch(
        exam_id=exam.id,
        uploaded_file_name="scan.pdf",
        uploaded_file_path="x",
        total_pages=2,
    )
    matched_page = ScannedPage(
        scan_batch=batch,
        sheet_instance=instance_a,
        student_id=student_a.id,
        physical_sheet_id=sheet.id,
        page_no=1,
        image_path="x",
        qr_token="token-a",
        qr_status="manual",
        review_status="ok",
    )
    reviewed_response = Response(
        scanned_page=matched_page,
        question=question,
        detected_answer=None,
        final_answer="1",
        status="reviewed",
    )
    unmatched_page = ScannedPage(
        scan_batch=batch,
        page_no=2,
        image_path="x",
        qr_token="token-b",
        qr_status="invalid",
    )
    session.add_all(
        [instance_a, instance_b, batch, matched_page, reviewed_response, unmatched_page]
    )
    session.commit()
    return {
        "exam": exam,
        "batch": batch,
        "matched_page": matched_page,
        "reviewed_response": reviewed_response,
        "unmatched_page": unmatched_page,
        "instance_b": instance_b,
    }


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        yield db


def test_process_scan_batch_refuses_to_wipe_manual_work(session) -> None:
    data = _scan_fixture(session)

    with pytest.raises(ValueError, match="manual mappings or review corrections"):
        process_scan_batch(session, data["batch"])

    # Nothing was deleted by the refused call.
    assert session.get(Response, data["reviewed_response"].id) is not None
    assert len(data["batch"].scanned_pages) == 2


def test_batch_has_protected_work_detects_manual_and_reviewed(session) -> None:
    data = _scan_fixture(session)
    assert batch_has_protected_work(data["batch"]) is True

    fresh_batch = ScanBatch(
        exam_id=data["exam"].id, uploaded_file_name="b.pdf", uploaded_file_path="y"
    )
    session.add(fresh_batch)
    session.commit()
    assert batch_has_protected_work(fresh_batch) is False


def test_reprocess_unmatched_pages_preserves_manual_work_and_matches_new(
    session, monkeypatch
) -> None:
    data = _scan_fixture(session)
    monkeypatch.setattr(scan_batch_service, "align_scanned_page", lambda page, instance: None)
    monkeypatch.setattr(
        scan_batch_service, "_read_responses_for_sheet", lambda db, page, instance: 1
    )

    summary = reprocess_unmatched_pages(session, data["batch"])

    # The manually mapped page and its reviewed response are untouched.
    assert data["matched_page"].sheet_instance_id is not None
    assert data["matched_page"].qr_status == "manual"
    surviving = session.get(Response, data["reviewed_response"].id)
    assert surviving is not None
    assert surviving.status == "reviewed"
    assert surviving.final_answer == "1"
    # The previously unmatched page is now matched via its stored QR token.
    assert data["unmatched_page"].sheet_instance_id == data["instance_b"].id
    assert data["unmatched_page"].qr_status == "ok"
    assert summary.detected_pages == 2
    assert summary.problem_pages == 0
    assert data["batch"].status == "processed"


def test_reprocess_wrong_exam_qr_becomes_assignable_problem_page(session) -> None:
    data = _scan_fixture(session)
    other_exam = MockExam(name="Other", assessment_type="generic_test")
    other_sheet = PhysicalSheet(sheet_no=1, sheet_name="Other Sheet", slug="other")
    other_student = Student(student_number="OTH001", name="Other Student", class_name="B")
    other_exam.physical_sheets.append(other_sheet)
    session.add_all([other_exam, other_student])
    session.flush()
    other_template = SheetTemplate(exam_id=other_exam.id, physical_sheet_id=other_sheet.id)
    session.add(other_template)
    session.flush()
    other_instance = SheetInstance(
        exam_id=other_exam.id,
        student_id=other_student.id,
        physical_sheet_id=other_sheet.id,
        sheet_template_id=other_template.id,
        qr_token="wrong-exam-token",
        printed_name=other_student.name,
        printed_student_number=other_student.student_number,
    )
    data["unmatched_page"].qr_token = "wrong-exam-token"
    session.add(other_instance)
    session.commit()

    summary = reprocess_unmatched_pages(session, data["batch"])

    assert data["unmatched_page"].sheet_instance_id is None
    assert data["unmatched_page"].qr_status == "wrong_exam"
    assert data["unmatched_page"].review_status == "needs_review"
    assert data["unmatched_page"].source_exam_id == other_exam.id
    assert data["unmatched_page"].source_sheet_instance_id == other_instance.id
    assert summary.problem_pages == 1


def test_duplicate_scan_pending_when_existing_has_reviewed_correction(session, monkeypatch) -> None:
    data = _scan_fixture(session)
    duplicate = ScannedPage(
        scan_batch=data["batch"],
        sheet_instance=data["matched_page"].sheet_instance,
        student_id=data["matched_page"].student_id,
        physical_sheet_id=data["matched_page"].physical_sheet_id,
        page_no=3,
        image_path="x",
        qr_token="token-a",
        qr_status="ok",
        review_status="ok",
    )
    session.add(duplicate)
    session.flush()
    monkeypatch.setattr(
        scan_batch_service.cv2, "imread", lambda *args, **kwargs: np.zeros((10, 10))
    )

    removed = scan_batch_service._handle_duplicate_page(
        session, duplicate, data["matched_page"].sheet_instance
    )

    assert removed == 0
    assert duplicate.qr_status == "duplicate_pending"
    assert duplicate.review_status == "duplicate_pending"
    assert duplicate.duplicate_of_page_id == data["matched_page"].id


@pytest.mark.parametrize("extension", [".jpg", ".jpeg", ".png"])
def test_load_image_for_omr_reads_camera_image_formats(tmp_path, extension) -> None:
    """Camera/phone scans keep working whether they arrive as JPG, JPEG or PNG."""
    image = np.full((16, 12, 3), 128, dtype=np.uint8)
    path = tmp_path / f"scan{extension}"
    assert scan_batch_service.cv2.imwrite(str(path), image)

    loaded = image_loading_service.load_image_for_omr(path, context=f"test_{extension}")

    assert loaded is not None
    assert loaded.shape == image.shape


def test_load_image_for_omr_reads_normal_png(tmp_path) -> None:
    image = np.full((12, 10, 3), 255, dtype=np.uint8)
    path = tmp_path / "scan.png"
    assert scan_batch_service.cv2.imwrite(str(path), image)

    loaded = image_loading_service.load_image_for_omr(path, context="test_png")

    assert loaded is not None
    assert loaded.shape == image.shape


def test_save_image_for_omr_writes_normal_png(tmp_path) -> None:
    image = np.full((12, 10, 3), 255, dtype=np.uint8)
    path = tmp_path / "nested" / "scan.png"

    saved = image_loading_service.save_image_for_omr(path, image, context="test_save")

    assert saved is True
    assert path.exists()
    assert path.stat().st_size > 0


def test_save_image_for_omr_uses_imencode_fallback(monkeypatch, tmp_path) -> None:
    image = np.full((12, 10, 3), 255, dtype=np.uint8)
    path = tmp_path / "fallback.png"

    monkeypatch.setattr(image_loading_service.cv2, "imwrite", lambda *_args: False)

    saved = image_loading_service.save_image_for_omr(
        path,
        image,
        context="test_save_fallback",
    )

    assert saved is True
    assert path.exists()
    assert path.stat().st_size > 0


def test_load_image_for_omr_returns_none_for_missing_file(tmp_path) -> None:
    loaded = image_loading_service.load_image_for_omr(
        tmp_path / "missing.png",
        context="test_missing",
    )

    assert loaded is None


def test_load_image_for_omr_uses_fromfile_fallback(monkeypatch, tmp_path) -> None:
    path = tmp_path / "fallback.png"
    path.write_bytes(b"encoded")
    expected = np.zeros((4, 5, 3), dtype=np.uint8)

    monkeypatch.setattr(image_loading_service.cv2, "imread", lambda *_args: None)
    monkeypatch.setattr(image_loading_service.cv2, "imdecode", lambda *_args: expected)

    result = image_loading_service.inspect_image_loading_for_omr(
        path,
        context="test_fallback",
    )

    assert result.image is expected
    assert result.diagnostics.imread_worked is False
    assert result.diagnostics.fromfile_worked is True
    assert result.diagnostics.byte_length == len(b"encoded")
    assert result.diagnostics.imdecode_worked is True


def test_align_scanned_page_does_not_keep_missing_aligned_path(
    session,
    monkeypatch,
) -> None:
    data = _scan_fixture(session)
    page = data["matched_page"]
    image = np.full((20, 20, 3), 255, dtype=np.uint8)
    page.image_path = "processed_pages/source.png"
    page.aligned_image_path = None
    page.alignment_status = "failed"
    page.review_status = "ok"

    monkeypatch.setattr(scan_batch_service, "load_image_for_omr", lambda *_args, **_kwargs: image)
    monkeypatch.setattr(
        scan_batch_service,
        "align_page",
        lambda *_args, **_kwargs: SimpleNamespace(
            image=image,
            status="ok",
            detected_markers={},
        ),
    )
    monkeypatch.setattr(scan_batch_service, "save_image_for_omr", lambda *_args, **_kwargs: False)

    scan_batch_service.align_scanned_page(page, page.sheet_instance)

    assert page.alignment_status == "failed"
    assert page.aligned_image_path == page.image_path
    assert page.review_status == "needs_review"


def test_read_responses_marks_page_needs_review_when_image_missing(session) -> None:
    data = _scan_fixture(session)
    page = data["matched_page"]
    page.image_path = "missing.png"
    page.aligned_image_path = None
    page.review_status = "ok"
    session.commit()

    created = scan_batch_service._read_responses_for_sheet(
        session,
        page,
        page.sheet_instance,
    )

    assert created == 0
    assert page.review_status == "needs_review"


def test_read_responses_falls_back_to_original_when_aligned_image_missing(
    session,
) -> None:
    data = _scan_fixture(session)
    page = data["matched_page"]
    image = np.full((100, 100), 255, dtype=np.uint8)
    path = get_settings().data_root / "processed_pages" / "fallback_original.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    assert scan_batch_service.cv2.imwrite(str(path), image)
    page.image_path = "processed_pages/fallback_original.png"
    page.aligned_image_path = "processed_pages/missing_aligned.png"
    page.review_status = "ok"
    page.sheet_instance.sheet_template.coordinate_map_json = {
        "page_width_mm": 100,
        "page_height_mm": 100,
        "questions": [
            {
                "question_id": data["reviewed_response"].question_id,
                "choices": [
                    {"choice": "1", "x_mm": 30, "y_mm": 30, "radius_mm": 3},
                    {"choice": "2", "x_mm": 45, "y_mm": 30, "radius_mm": 3},
                ],
            }
        ],
    }
    session.commit()

    created = scan_batch_service._read_responses_for_sheet(
        session,
        page,
        page.sheet_instance,
    )

    assert created == 1


def test_read_responses_relinks_stale_coordinate_question_id(session) -> None:
    data = _scan_fixture(session)
    page = data["matched_page"]
    question = data["reviewed_response"].question
    question.local_question_label = "3.2"
    question.problem_no = 5
    image = np.full((100, 100), 255, dtype=np.uint8)
    path = get_settings().data_root / "processed_pages" / "stale_question.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    assert scan_batch_service.cv2.imwrite(str(path), image)
    page.image_path = "processed_pages/stale_question.png"
    page.aligned_image_path = None
    page.review_status = "ok"
    page.sheet_instance.sheet_template.coordinate_map_json = {
        "page_width_mm": 100,
        "page_height_mm": 100,
        "questions": [
            {
                "question_id": 999999,
                "display_label": "3.2",
                "group_label": "",
                "choices": [
                    {"choice": "1", "x_mm": 30, "y_mm": 30, "radius_mm": 3},
                    {"choice": "2", "x_mm": 45, "y_mm": 30, "radius_mm": 3},
                    {"choice": "3", "x_mm": 60, "y_mm": 30, "radius_mm": 3},
                    {"choice": "4", "x_mm": 75, "y_mm": 30, "radius_mm": 3},
                ],
            }
        ],
        "groups": [{"group_label": "", "question_ids": [999999]}],
    }
    session.query(Response).filter(Response.scanned_page_id == page.id).delete()
    session.commit()

    created = scan_batch_service._read_responses_for_sheet(
        session,
        page,
        page.sheet_instance,
    )
    session.flush()
    response = session.query(Response).filter(Response.scanned_page_id == page.id).one()

    assert created == 1
    assert response.question_id == question.id
    assert (
        page.sheet_instance.sheet_template.coordinate_map_json["questions"][0]["question_id"]
        == question.id
    )


def test_read_responses_marks_page_needs_review_when_coordinates_empty(session) -> None:
    data = _scan_fixture(session)
    page = data["matched_page"]
    image = np.full((12, 10), 255, dtype=np.uint8)
    path = get_settings().data_root / "empty_coords.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    assert scan_batch_service.cv2.imwrite(str(path), image)
    page.image_path = "empty_coords.png"
    page.aligned_image_path = None
    page.review_status = "ok"
    page.sheet_instance.sheet_template.coordinate_map_json = {}
    session.commit()

    created = scan_batch_service._read_responses_for_sheet(
        session,
        page,
        page.sheet_instance,
    )

    assert created == 0
    assert page.review_status == "needs_review"


@pytest.mark.parametrize(
    ("response_statuses", "expected_status", "expected_reason"),
    [
        (["ok", "ok"], "ok", "all_responses_ok"),
        (["unclear"], "needs_review", "reviewable_responses"),
        (["blank"], "needs_review", "reviewable_responses"),
        (["multiple_mark"], "needs_review", "reviewable_responses"),
        (["invalid"], "needs_review", "reviewable_responses"),
    ],
)
def test_normalize_page_review_status_uses_actual_response_statuses(
    session,
    response_statuses,
    expected_status,
    expected_reason,
) -> None:
    data = _scan_fixture(session)
    page = data["unmatched_page"]
    page.sheet_instance = data["instance_b"]
    page.student_id = data["instance_b"].student_id
    page.physical_sheet_id = data["instance_b"].physical_sheet_id
    page.review_status = "needs_review"
    page.alignment_status = "weak"
    session.query(Response).filter(Response.scanned_page_id == page.id).delete()
    session.flush()
    for status in response_statuses:
        session.add(
            Response(
                scanned_page_id=page.id,
                question_id=data["reviewed_response"].question_id,
                status=status,
            )
        )

    refresh_page_responses_for_status(session, page)
    reason = normalize_page_review_status(page)

    assert reason == expected_reason
    assert page.review_status == expected_status


def test_normalize_page_review_status_needs_review_without_sheet_instance(session) -> None:
    data = _scan_fixture(session)
    page = data["unmatched_page"]
    page.review_status = "ok"

    reason = normalize_page_review_status(page)

    assert reason == "missing_sheet_instance"
    assert page.review_status == "needs_review"


def test_normalize_page_review_status_needs_review_without_responses(session) -> None:
    data = _scan_fixture(session)
    page = data["unmatched_page"]
    page.sheet_instance = data["instance_b"]
    page.student_id = data["instance_b"].student_id
    page.physical_sheet_id = data["instance_b"].physical_sheet_id
    page.review_status = "ok"
    page.alignment_status = "weak"
    session.query(Response).filter(Response.scanned_page_id == page.id).delete()
    refresh_page_responses_for_status(session, page)

    reason = normalize_page_review_status(page)

    assert reason == "no_responses"
    assert page.review_status == "needs_review"
