import json
import logging
import os
from ast import literal_eval
from hashlib import sha256
from pathlib import Path
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

import cv2
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi import Response as FastAPIResponse
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    BlankSheetLabel,
    Response,
    ScanBatch,
    ScannedPage,
    SheetInstance,
    SheetTemplate,
    StudentResult,
)
from app.services.archive_service import is_purged_scan_path
from app.services.demo_service import ensure_uploads_allowed
from app.services.exam_service import get_run_or_404
from app.services.image_loading_service import inspect_image_loading_for_omr
from app.services.qr_token_service import generate_qr_token
from app.services.scan_batch_service import (
    _read_responses_for_sheet,
    align_scanned_page,
    create_scan_batch,
    normalize_page_review_status,
    normalize_qr_token,
    process_scan_batch,
    refresh_page_responses_for_status,
    reprocess_unmatched_pages,
)
from app.services.scoring_service import maybe_finalize_exam
from app.services.templates import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/exams/{exam_id}/scan-batches", response_class=HTMLResponse)
def scan_batches(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id)
    return templates.TemplateResponse(
        request,
        "scans/index.html",
        {
            "request": request,
            "active_nav": "scan_review",
            "exam": exam,
            "page_rows": _page_rows(exam),
            "included_links": [link for link in exam.students if link.included],
            "physical_sheets": exam.physical_sheets,
            "upload_errors": request.query_params.getlist("error"),
        },
    )


@router.get("/exams/{exam_id}/scan-batches/new")
def new_scan_batch(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    get_run_or_404(db, exam_id)
    return RedirectResponse(f"/exams/{exam_id}/scan-batches", status_code=303)


@router.post("/scanned-pages/{page_id}/map", response_model=None)
def map_scanned_page(
    page_id: int,
    student_id: int = Form(...),
    physical_sheet_id: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    page = db.get(ScannedPage, page_id)
    if page is None:
        raise HTTPException(status_code=404)
    exam = get_run_or_404(db, page.scan_batch.exam_id, for_write=True)
    student_link = next(
        (link for link in exam.students if link.included and link.student_id == student_id),
        None,
    )
    physical_sheet = next(
        (sheet for sheet in exam.physical_sheets if sheet.id == physical_sheet_id),
        None,
    )
    if student_link is None or physical_sheet is None:
        raise HTTPException(status_code=400, detail="Choose a valid student and sheet.")

    sheet_template = db.scalar(
        select(SheetTemplate)
        .where(SheetTemplate.exam_id == exam.id)
        .where(SheetTemplate.physical_sheet_id == physical_sheet.id)
        .order_by(SheetTemplate.created_at.desc())
    )
    if sheet_template is None:
        raise HTTPException(status_code=400, detail="Generate mark sheets before manual mapping.")

    # If this page came from a printed blank sheet, keep its printed QR token:
    # the token on paper stays the token of the final assigned sheet instance,
    # so rescans of the same paper match the student automatically.
    blank_label = None
    if page.qr_token:
        blank_label = db.scalar(
            select(BlankSheetLabel)
            .where(BlankSheetLabel.qr_token == page.qr_token)
            .where(BlankSheetLabel.exam_id == exam.id)
            .where(BlankSheetLabel.physical_sheet_id == physical_sheet_id)
        )
        if blank_label is not None and db.scalar(
            select(SheetInstance).where(SheetInstance.qr_token == blank_label.qr_token)
        ):
            blank_label = None  # token already belongs to an assigned instance

    sheet_instance = db.scalar(
        select(SheetInstance)
        .where(SheetInstance.exam_id == exam.id)
        .where(SheetInstance.student_id == student_id)
        .where(SheetInstance.physical_sheet_id == physical_sheet_id)
    )
    if sheet_instance is None:
        sheet_instance = SheetInstance(
            exam_id=exam.id,
            student_id=student_id,
            physical_sheet_id=physical_sheet_id,
            sheet_template_id=blank_label.sheet_template_id if blank_label else sheet_template.id,
            qr_token=blank_label.qr_token if blank_label else generate_qr_token(),
            printed_name=student_link.student.name,
            printed_student_number=student_link.student.student_number,
            page_no=page.page_no,
            status="generated",
        )
        db.add(sheet_instance)
        db.flush()
    elif blank_label is not None and not any(
        scanned.id != page.id for scanned in sheet_instance.scanned_pages
    ):
        # The student's pre-printed sheet was never scanned; the blank sheet on
        # paper is the one actually used, so adopt its token and layout.
        sheet_instance.qr_token = blank_label.qr_token
        sheet_instance.sheet_template = blank_label.sheet_template

    page.sheet_instance_id = sheet_instance.id
    page.student_id = student_id
    page.physical_sheet_id = physical_sheet_id
    original_status = page.qr_status
    page.qr_status = "manual"
    page.review_status = "ok"
    if original_status == "wrong_exam":
        page.mapping_note = "Manually assigned from a sheet printed for another test session."
    db.query(Response).filter(Response.scanned_page_id == page.id).delete()
    align_scanned_page(page, sheet_instance)
    responses_created = _read_responses_for_sheet(db, page, sheet_instance)
    refresh_page_responses_for_status(db, page)
    normalize_page_review_status(page)
    if responses_created == 0:
        page.review_status = "needs_review"
        page.mapping_note = "回答を読み取れませんでした。画像またはテンプレートを確認してください。"
        logger.warning(
            "Manual scan mapping created zero OMR responses",
            extra={
                "manual_scan_mapping": {
                    "page_id": page.id,
                    "student_id": student_id,
                    "physical_sheet_id": physical_sheet_id,
                    "sheet_instance_id": sheet_instance.id,
                    "sheet_template_id": sheet_instance.sheet_template_id,
                    "responses_created": responses_created,
                }
            },
        )
    batch = page.scan_batch
    batch.detected_pages = sum(
        1 for scanned_page in batch.scanned_pages if scanned_page.sheet_instance_id
    )
    batch.problem_pages = sum(
        1 for scanned_page in batch.scanned_pages if not scanned_page.sheet_instance_id
    )
    batch.status = "needs_review" if batch.problem_pages else "processed"
    db.commit()
    maybe_finalize_exam(db, exam)
    return RedirectResponse(f"/exams/{exam.id}/scan-batches", status_code=303)


@router.post("/scanned-pages/{page_id}/duplicates/replace", response_model=None)
def replace_duplicate_page(page_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    page = db.get(ScannedPage, page_id)
    if page is None or page.duplicate_of_page_id is None:
        raise HTTPException(status_code=404)
    old_page = db.get(ScannedPage, page.duplicate_of_page_id)
    if old_page is None:
        raise HTTPException(status_code=404)
    exam = get_run_or_404(db, page.scan_batch.exam_id, for_write=True)
    old_page.review_status = "replaced"
    old_page.replaced_by_page_id = page.id
    page.review_status = "ok"
    page.qr_status = "ok"
    page.mapping_note = f"Teacher replaced scan page {old_page.id} with this page."
    db.commit()
    maybe_finalize_exam(db, exam)
    return RedirectResponse(f"/exams/{exam.id}/scan-batches", status_code=303)


@router.post("/scanned-pages/{page_id}/duplicates/keep-old", response_model=None)
def keep_old_duplicate_page(page_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    page = db.get(ScannedPage, page_id)
    if page is None:
        raise HTTPException(status_code=404)
    exam = get_run_or_404(db, page.scan_batch.exam_id, for_write=True)
    page.review_status = "ignored_duplicate"
    page.qr_status = "ignored_duplicate"
    page.mapping_note = "Teacher kept the existing scan and ignored this duplicate."
    db.query(Response).filter(Response.scanned_page_id == page.id).delete()
    db.commit()
    maybe_finalize_exam(db, exam)
    return RedirectResponse(f"/exams/{exam.id}/scan-batches", status_code=303)


@router.post("/exams/{exam_id}/scan-batches/upload")
async def upload_scan_batch(
    exam_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ensure_uploads_allowed()
    exam = get_run_or_404(db, exam_id, for_write=True)
    errors: list[str] = []
    for file in files:
        if not file.filename:
            continue
        content = await file.read()
        if not content:
            continue
        try:
            batch = create_scan_batch(db, exam_id, file.filename, content)
            process_scan_batch(db, batch)
        except (ValueError, Exception) as exc:
            errors.append(f"{file.filename}: {exc}")
    maybe_finalize_exam(db, exam)
    if errors:
        params = "&".join(f"error={e}" for e in errors[:3])
        return RedirectResponse(f"/exams/{exam_id}/scan-batches?{params}", status_code=303)
    return RedirectResponse(f"/exams/{exam_id}/scan-batches", status_code=303)


@router.post("/exams/{exam_id}/scans/reprocess", response_model=None)
def reprocess_exam_scans(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Safe reprocess: retry QR matching/reading for unmatched pages only."""
    exam = get_run_or_404(db, exam_id, for_write=True)
    errors: list[str] = []
    for batch in exam.scan_batches:
        try:
            reprocess_unmatched_pages(db, batch)
        except ValueError as exc:
            errors.append(f"{batch.uploaded_file_name}: {exc}")
    maybe_finalize_exam(db, exam)
    if errors:
        params = "&".join(f"error={e}" for e in errors[:3])
        return RedirectResponse(f"/exams/{exam_id}/scan-batches?{params}", status_code=303)
    return RedirectResponse(f"/exams/{exam_id}/scan-batches", status_code=303)


@router.post("/exams/{exam_id}/scans/reprocess-full", response_model=None)
def reprocess_exam_scans_full(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    """Destructive reprocess from the original uploads; discards manual work."""
    exam = get_run_or_404(db, exam_id, for_write=True)
    errors: list[str] = []
    for batch in exam.scan_batches:
        try:
            process_scan_batch(db, batch, force=True)
        except ValueError as exc:
            errors.append(f"{batch.uploaded_file_name}: {exc}")
    maybe_finalize_exam(db, exam)
    if errors:
        params = "&".join(f"error={e}" for e in errors[:3])
        return RedirectResponse(f"/exams/{exam_id}/scan-batches?{params}", status_code=303)
    return RedirectResponse(f"/exams/{exam_id}/scan-batches", status_code=303)


@router.get("/debug/latest-scan-pages.json")
def latest_scan_pages_debug(request: Request, db: Session = Depends(get_db)) -> dict:
    _assert_scan_debug_allowed(request)
    pages = (
        db.scalars(select(ScannedPage).order_by(ScannedPage.created_at.desc()).limit(20))
        .unique()
        .all()
    )
    return {"pages": [_scan_page_debug_summary(db, page) for page in pages]}


@router.get("/debug/scanned-page/{page_id}.json")
def scanned_page_debug(page_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    _assert_scan_debug_allowed(request)
    page = db.get(ScannedPage, page_id)
    if page is None:
        raise HTTPException(status_code=404)
    return _scan_page_debug_detail(db, page)


@router.get("/debug/qr/pages.json")
def qr_debug_pages(request: Request, db: Session = Depends(get_db)) -> dict:
    _assert_scan_debug_allowed(request)
    pages = (
        db.scalars(select(ScannedPage).order_by(ScannedPage.created_at.desc()).limit(100))
        .unique()
        .all()
    )
    return {"pages": [_qr_debug_summary_for_page(db, page) for page in pages]}


@router.get("/debug/qr/scanned-page/{page_id}.json")
def qr_debug_scanned_page(page_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    _assert_scan_debug_allowed(request)
    page = db.get(ScannedPage, page_id)
    if page is None:
        raise HTTPException(status_code=404)
    return _qr_debug_summary_for_page(db, page, include_attempts=True)


@router.get("/debug/qr/batch/{batch_id}/page/{page_no}.json")
def qr_debug_batch_page(
    batch_id: int,
    page_no: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _assert_scan_debug_allowed(request)
    page = db.scalar(
        select(ScannedPage)
        .where(ScannedPage.scan_batch_id == batch_id)
        .where(ScannedPage.page_no == page_no)
    )
    if page:
        return _qr_debug_summary_for_page(db, page, include_attempts=True)
    return _qr_debug_summary_from_sidecar(db, batch_id, page_no, include_attempts=True)


@router.post("/debug/review/exam/{exam_id}/normalize-statuses")
def normalize_review_statuses_debug(
    exam_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    _assert_scan_debug_allowed(request)
    pages = list(
        db.scalars(
            select(ScannedPage)
            .join(ScanBatch)
            .where(ScanBatch.exam_id == exam_id)
            .where(ScannedPage.review_status.in_(("ok", "needs_review")))
        ).unique()
    )
    rows = []
    changed = 0
    for page in pages:
        before = page.review_status
        refresh_page_responses_for_status(db, page)
        reason = normalize_page_review_status(page)
        if page.review_status != before:
            changed += 1
        rows.append(
            {
                "scanned_page_id": page.id,
                "before": before,
                "after": page.review_status,
                "reason": reason,
                "response_count": len(page.responses),
            }
        )
    db.commit()
    return {
        "exam_id": exam_id,
        "page_count": len(rows),
        "changed_page_count": changed,
        "pages": rows,
    }


@router.get("/scanned-pages/{page_id}/debug-overlay")
def scanned_page_debug_overlay(page_id: int, db: Session = Depends(get_db)) -> FastAPIResponse:
    """Development aid: expected bubble/marker positions drawn on the page image."""
    from app.services.alignment_service import marker_centers_mm
    from app.services.scan_batch_service import _page_dimensions_mm

    page = db.get(ScannedPage, page_id)
    if page is None or page.sheet_instance is None:
        raise HTTPException(status_code=404)
    path = _data_file_or_404(page.aligned_image_path or page.image_path)
    image = cv2.imread(str(path))
    if image is None:
        raise HTTPException(status_code=404)

    template = page.sheet_instance.sheet_template
    coordinate_map = template.coordinate_map_json or {}
    page_w_mm, page_h_mm = _page_dimensions_mm(coordinate_map, template.paper_size)
    height, width = image.shape[:2]
    scale_x = width / page_w_mm
    scale_y = height / page_h_mm

    detected_by_question: dict[int, str | None] = {
        r.question_id: r.detected_answer for r in page.responses
    }
    review_questions = {
        r.question_id
        for r in page.responses
        if r.status in {"blank", "multiple_mark", "unclear", "invalid"}
    }
    for question in coordinate_map.get("questions", []):
        question_id = question.get("question_id")
        for choice in question.get("choices", []):
            center = (int(choice["x_mm"] * scale_x), int(choice["y_mm"] * scale_y))
            radius = max(3, int(choice["radius_mm"] * scale_x))
            if detected_by_question.get(question_id) == choice["choice"]:
                color, thickness = (0, 180, 0), 2
            elif question_id in review_questions:
                color, thickness = (0, 0, 220), 1
            else:
                color, thickness = (220, 120, 0), 1
            cv2.circle(image, center, radius, color, thickness)

    markers = template.marker_positions_json or coordinate_map.get("marker_positions") or {}
    half_px = int(3 * scale_x)
    for cx_mm, cy_mm in marker_centers_mm(markers).values():
        center = (int(cx_mm * scale_x), int(cy_mm * scale_y))
        cv2.rectangle(
            image,
            (center[0] - half_px, center[1] - half_px),
            (center[0] + half_px, center[1] + half_px),
            (0, 0, 255),
            2,
        )
    cv2.putText(
        image,
        f"alignment: {page.alignment_status}",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 0, 255),
        2,
    )

    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise HTTPException(status_code=500, detail="Could not render debug overlay.")
    return FastAPIResponse(content=encoded.tobytes(), media_type="image/png")


@router.get("/scan-batches/{batch_id}/uploaded-file")
def uploaded_scan_file(batch_id: int, db: Session = Depends(get_db)) -> FileResponse:
    batch = db.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    path = _data_file_or_404(batch.uploaded_file_path)
    return FileResponse(
        path,
        filename=batch.uploaded_file_name,
        content_disposition_type="inline",
    )


@router.get("/scan-batches/{batch_id}/uploaded-file/download")
def download_uploaded_scan_file(batch_id: int, db: Session = Depends(get_db)) -> FileResponse:
    batch = db.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    path = _data_file_or_404(batch.uploaded_file_path)
    return FileResponse(path, filename=batch.uploaded_file_name)


@router.get("/scanned-pages/{page_id}/image")
def scanned_page_image(page_id: int, db: Session = Depends(get_db)) -> FileResponse:
    page = db.get(ScannedPage, page_id)
    if page is None:
        raise HTTPException(status_code=404)
    path = _data_file_or_404(page.aligned_image_path or page.image_path)
    return FileResponse(
        path,
        filename=Path(path).name,
        content_disposition_type="inline",
    )


@router.get("/scanned-pages/{page_id}/image/download")
def download_scanned_page_image(page_id: int, db: Session = Depends(get_db)) -> FileResponse:
    page = db.get(ScannedPage, page_id)
    if page is None:
        raise HTTPException(status_code=404)
    path = _data_file_or_404(page.aligned_image_path or page.image_path)
    return FileResponse(path, filename=f"scan_page_{page.page_no:04}{path.suffix}")


@router.get("/scan-batches/{batch_id}/processed-pages.zip")
def download_processed_pages(batch_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    batch = db.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    if not batch.scanned_pages:
        raise HTTPException(status_code=404)

    archive_file = SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    with ZipFile(archive_file, "w", ZIP_DEFLATED) as archive:
        for page in batch.scanned_pages:
            path = _data_file_or_404(page.aligned_image_path or page.image_path)
            archive.write(path, arcname=f"page_{page.page_no:04}{path.suffix}")
    archive_file.seek(0)

    return StreamingResponse(
        archive_file,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="scan_batch_{batch.id}_processed_pages.zip"'
        },
    )


@router.post("/scan-batches/{batch_id}/delete", response_model=None)
def delete_scan_batch(
    batch_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    batch = db.get(ScanBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404)
    exam_id = batch.exam_id
    get_run_or_404(db, exam_id, for_write=True)  # archived sessions are read-only
    data_root = get_settings().data_root.resolve()

    for page in batch.scanned_pages:
        for rel in (page.image_path, page.aligned_image_path):
            if rel:
                p = (data_root / rel).resolve()
                if data_root in p.parents and p.is_file():
                    p.unlink(missing_ok=True)

    if batch.uploaded_file_path:
        p = (data_root / batch.uploaded_file_path).resolve()
        if data_root in p.parents and p.is_file():
            p.unlink(missing_ok=True)

    db.query(StudentResult).filter(StudentResult.exam_id == exam_id).delete()
    db.delete(batch)
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/scan-batches", status_code=303)


def _assert_scan_debug_allowed(request: Request) -> None:
    if not _scan_debug_enabled():
        raise HTTPException(status_code=404)
    client_host = request.client.host if request.client else ""
    host_header = request.headers.get("host", "").split(":", 1)[0]
    allowed_hosts = {"127.0.0.1", "::1", "localhost"}
    if client_host not in allowed_hosts and host_header not in allowed_hosts:
        raise HTTPException(status_code=404)


def _scan_debug_enabled() -> bool:
    env_value = os.getenv("ENABLE_SCAN_DEBUG")
    if env_value is not None:
        return env_value == "1"
    return get_settings().enable_scan_debug


def _scan_page_debug_summary(db: Session, page: ScannedPage) -> dict:
    sheet_instance = page.sheet_instance
    template = sheet_instance.sheet_template if sheet_instance else None
    student = sheet_instance.student if sheet_instance else None
    physical_sheet = sheet_instance.physical_sheet if sheet_instance else None
    result = _student_result_for_page(db, page)
    status_counts = _response_status_counts(page.responses)
    reviewable_count = _reviewable_response_count(status_counts)
    return {
        "scanned_page_id": page.id,
        "scan_batch_id": page.scan_batch_id,
        "exam_id": page.scan_batch.exam_id,
        "page_number": page.page_no,
        "student_id": page.student_id,
        "student_number": student.student_number if student else None,
        "student_name": student.name if student else None,
        "physical_sheet_id": page.physical_sheet_id,
        "physical_sheet_name": physical_sheet.sheet_name if physical_sheet else None,
        "sheet_instance_id": page.sheet_instance_id,
        "sheet_template_id": template.id if template else None,
        "template_name": physical_sheet.sheet_name if physical_sheet else None,
        "qr_status": page.qr_status,
        "qr_token_exists": bool(page.qr_token),
        "review_status": page.review_status,
        "page_review_status_reason": _page_review_status_reason(page, status_counts),
        "alignment_status": page.alignment_status,
        "image_path": page.image_path,
        "aligned_image_path": page.aligned_image_path,
        "response_count": len(page.responses),
        "response_status_counts": status_counts,
        "reviewable_response_count": reviewable_count,
        "non_ok_response_count": sum(
            count for status, count in status_counts.items() if status != "ok"
        ),
        "all_responses_ok": bool(page.responses) and reviewable_count == 0,
        "score": result.total_score if result else None,
        "max_score": result.score_details_json.get("max_points") if result else None,
    }


def _scan_page_debug_detail(db: Session, page: ScannedPage) -> dict:
    summary = _scan_page_debug_summary(db, page)
    template = page.sheet_instance.sheet_template if page.sheet_instance else None
    coordinate_map = template.coordinate_map_json if template else {}
    coordinate_map = coordinate_map or {}
    questions = coordinate_map.get("questions") or []
    original_path = get_settings().data_root / page.image_path
    aligned_path = (
        get_settings().data_root / page.aligned_image_path if page.aligned_image_path else None
    )
    original_load = inspect_image_loading_for_omr(
        original_path,
        context="scan_debug_original",
    ).diagnostics
    aligned_load = (
        inspect_image_loading_for_omr(
            aligned_path,
            context="scan_debug_aligned",
        ).diagnostics
        if aligned_path
        else None
    )
    response_count = len(page.responses)
    total_read_rows = response_count
    readable = aligned_load.loaded if aligned_load else original_load.loaded
    aligned_image_exists = aligned_path.exists() if aligned_path else False
    diagnosis = "Responses exist."
    if (
        total_read_rows == 0
        and aligned_path
        and not aligned_image_exists
        and page.alignment_status == "ok"
    ):
        diagnosis = "Alignment output file is missing. OpenCV image writing may have failed."
        if original_load.loaded:
            diagnosis = (
                f"{diagnosis} Aligned image is missing, but original image is readable. "
                "Response reading should fall back to the original image."
            )
    elif (
        total_read_rows == 0 and aligned_path and not aligned_image_exists and original_load.loaded
    ):
        diagnosis = (
            "Aligned image is missing, but original image is readable. "
            "Response reading should fall back to the original image."
        )
    elif total_read_rows == 0 and not readable:
        diagnosis = "OpenCV could not read the scan image."
    elif total_read_rows == 0 and not questions:
        diagnosis = "Sheet template has no readable OMR coordinates."
    elif total_read_rows == 0 and readable and questions:
        diagnosis = (
            "Image and template coordinates exist, but no responses were created. "
            "Check OMR reading loop, thresholds, or template/page mismatch."
        )

    return {
        **summary,
        "original_image_path": page.image_path,
        "aligned_image_path": page.aligned_image_path,
        "original_image_exists": original_path.exists(),
        "original_image_size": original_path.stat().st_size if original_path.exists() else None,
        "aligned_image_exists": aligned_image_exists,
        "aligned_image_size": (
            aligned_path.stat().st_size if aligned_path and aligned_path.exists() else None
        ),
        "opencv_original_image_readable": original_load.imread_worked,
        "original_image_shape": (
            list(original_load.image_shape) if original_load.image_shape else None
        ),
        "opencv_aligned_image_readable": aligned_load.imread_worked if aligned_load else None,
        "aligned_image_shape": (
            list(aligned_load.image_shape) if aligned_load and aligned_load.image_shape else None
        ),
        "original_fallback_worked": original_load.imdecode_worked,
        "aligned_fallback_worked": aligned_load.imdecode_worked if aligned_load else None,
        "original_image_loading": original_load.as_dict(),
        "aligned_image_loading": aligned_load.as_dict() if aligned_load else None,
        "coordinate_map_exists": bool(coordinate_map),
        "coordinate_map_keys": sorted(coordinate_map.keys()),
        "questions_count": len(questions),
        "marker_positions_exists": bool(
            (template.marker_positions_json if template else None)
            or coordinate_map.get("marker_positions")
        ),
        "page_width_height_mm": [
            coordinate_map.get("page_width_mm"),
            coordinate_map.get("page_height_mm"),
        ],
        "orientation": coordinate_map.get("orientation"),
        "response_count": response_count,
        "responses": [
            {
                "response_id": response.id,
                "question_id": response.question_id,
                "detected_answer": response.detected_answer,
                "final_answer": response.final_answer,
                "confidence": response.confidence,
                "status": response.status,
            }
            for response in page.responses[:20]
        ],
        "diagnosis": diagnosis,
    }


def _student_result_for_page(db: Session, page: ScannedPage) -> StudentResult | None:
    if page.student_id is None:
        return None
    return db.scalar(
        select(StudentResult).where(
            StudentResult.exam_id == page.scan_batch.exam_id,
            StudentResult.student_id == page.student_id,
        )
    )


def _response_status_counts(responses) -> dict:
    statuses = ["ok", "blank", "multiple_mark", "unclear", "invalid", "reviewed"]
    counts = {status: 0 for status in statuses}
    for response in responses:
        counts[response.status] = counts.get(response.status, 0) + 1
    return counts


def _reviewable_response_count(status_counts: dict) -> int:
    return sum(
        status_counts.get(status, 0) for status in {"blank", "multiple_mark", "unclear", "invalid"}
    )


def _page_review_status_reason(page: ScannedPage, status_counts: dict | None = None) -> str:
    status_counts = status_counts or _response_status_counts(page.responses)
    reviewable_count = _reviewable_response_count(status_counts)
    if page.review_status == "needs_review" and page.sheet_instance_id is None:
        return "missing_sheet_instance"
    if page.review_status == "needs_review" and page.alignment_status == "failed":
        return "alignment_failed"
    if page.review_status == "needs_review" and len(page.responses) == 0:
        return "no_responses"
    if page.review_status == "needs_review" and reviewable_count > 0:
        return "reviewable_responses"
    if page.review_status == "needs_review":
        return "needs_review_without_response_error"
    if page.review_status == "ok":
        return "all_responses_ok"
    return page.review_status


def _qr_sidecar_path(batch_id: int, page_no: int) -> Path:
    return (
        get_settings().data_root
        / "debug"
        / "qr_attempts"
        / f"batch_{batch_id}"
        / f"page_{page_no}_qr_debug.json"
    )


def _load_qr_sidecar(batch_id: int, page_no: int) -> dict | None:
    path = _qr_sidecar_path(batch_id, page_no)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _qr_debug_summary_for_page(
    db: Session,
    page: ScannedPage,
    include_attempts: bool = False,
) -> dict:
    return _qr_debug_summary_from_sidecar(
        db,
        page.scan_batch_id,
        page.page_no,
        page=page,
        include_attempts=include_attempts,
    )


def _qr_debug_summary_from_sidecar(
    db: Session,
    batch_id: int,
    page_no: int,
    page: ScannedPage | None = None,
    include_attempts: bool = False,
) -> dict:
    sidecar = _load_qr_sidecar(batch_id, page_no)
    attempts = sidecar.get("candidate_attempts", []) if sidecar else []
    points_detected = any(attempt.get("points_detected") for attempt in attempts)
    decoded = any(attempt.get("token_found") for attempt in attempts)
    candidate_dir = (
        get_settings().data_root
        / "debug"
        / "qr_candidates"
        / f"batch_{batch_id}"
        / f"page_{page_no}"
    )
    diagnosis = "QR debug info does not exist."
    if sidecar:
        diagnosis = sidecar.get("final_reason") or "QR debug info exists."
    token = _decoded_qr_token_from_debug(page, sidecar)
    payload = {
        "exists": sidecar is not None,
        "scanned_page_id": page.id if page else sidecar.get("scanned_page_id") if sidecar else None,
        "batch_id": batch_id,
        "exam_id": (
            page.scan_batch.exam_id if page else sidecar.get("exam_id") if sidecar else None
        ),
        "page_no": page_no,
        "candidate_crops_dir": str(candidate_dir) if candidate_dir.exists() else None,
        "candidate_count": sidecar.get("candidate_count", 0) if sidecar else 0,
        "total_attempts": len(attempts),
        "any_qr_points_detected": points_detected,
        "any_token_decoded": decoded,
        "final_qr_status": (
            page.qr_status if page else sidecar.get("final_qr_status") if sidecar else None
        ),
        "final_qr_token_exists": bool(
            page.qr_token if page else sidecar.get("final_token_preview") if sidecar else False
        ),
        "diagnosis": diagnosis,
        **_qr_token_match_diagnostics(db, token, page, sidecar),
    }
    if include_attempts:
        payload["debug"] = sidecar
    return payload


def _decoded_qr_token_from_debug(page: ScannedPage | None, sidecar: dict | None) -> str | None:
    if page and page.qr_token:
        return page.qr_token
    if not sidecar:
        return None
    token_repr = sidecar.get("final_token_repr")
    if isinstance(token_repr, str) and len(token_repr) >= 2:
        try:
            parsed = literal_eval(token_repr)
            return parsed if isinstance(parsed, str) else None
        except Exception:
            return token_repr[1:-1] if token_repr[:1] == token_repr[-1:] else token_repr
    return None


def _qr_token_match_diagnostics(
    db: Session,
    token: str | None,
    page: ScannedPage | None,
    sidecar: dict | None,
) -> dict:
    exam_id = page.scan_batch.exam_id if page else sidecar.get("exam_id") if sidecar else None
    normalized = normalize_qr_token(token) if token else None
    current_token = page.sheet_instance.qr_token if page and page.sheet_instance else None
    payload = {
        "decoded_token_length": len(token) if token is not None else None,
        "decoded_token_preview": _debug_token_preview(token),
        "decoded_token_sha256": _debug_token_sha256(token),
        "decoded_token_repr": repr(token) if token is not None else None,
        "normalized_token_length": len(normalized) if normalized is not None else None,
        "normalized_token_preview": _debug_token_preview(normalized),
        "exact_sheet_instance_match_any_exam": None,
        "exact_sheet_instance_match_current_exam": None,
        "exact_blank_label_match_current_exam": None,
        "normalized_sheet_instance_match_any_exam": None,
        "normalized_sheet_instance_match_current_exam": None,
        "normalized_blank_label_match_current_exam": None,
        "current_page_sheet_instance_token_preview": _debug_token_preview(current_token),
        "current_page_sheet_instance_token_sha256": _debug_token_sha256(current_token),
        "decoded_token_equals_current_sheet_instance_token": (
            token == current_token if token is not None and current_token is not None else None
        ),
    }
    if token is None:
        return payload
    payload["exact_sheet_instance_match_any_exam"] = _sheet_instance_token_exists(db, token)
    payload["exact_sheet_instance_match_current_exam"] = _sheet_instance_token_exists(
        db, token, exam_id
    )
    payload["exact_blank_label_match_current_exam"] = _blank_label_token_exists(db, token, exam_id)
    payload["normalized_sheet_instance_match_any_exam"] = _sheet_instance_token_exists(
        db, normalized
    )
    payload["normalized_sheet_instance_match_current_exam"] = _sheet_instance_token_exists(
        db, normalized, exam_id
    )
    payload["normalized_blank_label_match_current_exam"] = _blank_label_token_exists(
        db, normalized, exam_id
    )
    any_match = any(
        payload[key]
        for key in (
            "exact_sheet_instance_match_any_exam",
            "exact_blank_label_match_current_exam",
            "normalized_sheet_instance_match_any_exam",
            "normalized_blank_label_match_current_exam",
        )
    )
    if page and page.qr_status == "invalid" and not any_match:
        payload["diagnosis"] = (
            "QR token was decoded but did not match any known sheet instance or blank label."
        )
    return payload


def _sheet_instance_token_exists(
    db: Session, token: str | None, exam_id: int | None = None
) -> bool:
    if not token:
        return False
    query = select(SheetInstance.id).where(SheetInstance.qr_token == token)
    if exam_id is not None:
        query = query.where(SheetInstance.exam_id == exam_id)
    return db.scalar(query) is not None


def _blank_label_token_exists(db: Session, token: str | None, exam_id: int | None = None) -> bool:
    if not token:
        return False
    query = select(BlankSheetLabel.id).where(BlankSheetLabel.qr_token == token)
    if exam_id is not None:
        query = query.where(BlankSheetLabel.exam_id == exam_id)
    return db.scalar(query) is not None


def _debug_token_preview(token: str | None) -> str | None:
    if not token:
        return None
    if len(token) <= 12:
        return token
    return f"{token[:6]}...{token[-4:]}"


def _debug_token_sha256(token: str | None) -> str | None:
    if token is None:
        return None
    return sha256(token.encode("utf-8")).hexdigest()


def _page_rows(exam) -> list[dict]:
    sheets_by_id = {sheet.id: sheet for sheet in exam.physical_sheets}
    rows: list[dict] = []
    for batch in exam.scan_batches:
        for page in sorted(batch.scanned_pages, key=lambda p: p.page_no):
            si = page.sheet_instance
            sheet_name = "—"
            if si and si.physical_sheet:
                sheet_name = si.physical_sheet.sheet_name
            elif page.physical_sheet_id in sheets_by_id:
                sheet_name = sheets_by_id[page.physical_sheet_id].sheet_name
            total = len(page.responses)
            correct = sum(
                1
                for r in page.responses
                if r.final_answer is not None
                and r.question is not None
                and r.final_answer == r.question.correct_answer
            )
            needs_review = sum(
                1
                for r in page.responses
                if r.status in {"blank", "multiple_mark", "unclear", "invalid"}
                and r.question is not None
            )
            data_problem_count = sum(
                1
                for r in page.responses
                if r.status in {"blank", "multiple_mark", "unclear", "invalid"}
                and r.question is None
            )
            needs_review_responses = [
                r
                for r in page.responses
                if r.status in {"blank", "multiple_mark", "unclear", "invalid"}
                and r.question is not None
            ]
            data_problem_responses = [
                r
                for r in page.responses
                if r.status in {"blank", "multiple_mark", "unclear", "invalid"}
                and r.question is None
            ]
            rows.append(
                {
                    "page": page,
                    "student_name": si.printed_name if si else "—",
                    "student_number": si.printed_student_number if si else "—",
                    "sheet_name": sheet_name,
                    "total": total,
                    "correct": correct,
                    "needs_review": needs_review,
                    "data_problem_count": data_problem_count,
                    "first_review_id": needs_review_responses[0].id
                    if needs_review_responses
                    else None,
                    "first_data_problem_id": data_problem_responses[0].id
                    if data_problem_responses
                    else None,
                    "qr_matched": page.sheet_instance_id is not None,
                    "qr_status": page.qr_status,
                    "review_status": page.review_status,
                    "group": _row_group(page, needs_review, data_problem_count),
                }
            )
    return rows


def _row_group(page: ScannedPage, needs_review: int, data_problem_count: int = 0) -> str:
    if page.review_status == "duplicate_pending":
        return "duplicates"
    if page.sheet_instance_id is None:
        return "needs_assignment"
    if needs_review > 0 or data_problem_count > 0 or page.review_status == "needs_review":
        return "needs_check"
    return "ready"


def _data_file_or_404(relative_path: str | None) -> Path:
    if not relative_path:
        raise HTTPException(status_code=404)
    if is_purged_scan_path(relative_path):
        # The session was archived and its scans were deleted from disk.
        raise HTTPException(status_code=410, detail="Scan files were removed when archived.")
    data_root = get_settings().data_root.resolve()
    path = (data_root / relative_path).resolve()
    if data_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404)
    return path
