import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import cv2
import pypdfium2 as pdfium
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import data_relative_path, ensure_data_directories
from app.models import BlankSheetLabel, Response, ScanBatch, ScannedPage, SheetInstance
from app.services.alignment_service import align_page
from app.services.bubble_reader_service import classify_bubble_fills
from app.services.coordinate_question_service import (
    resolve_coordinate_question,
    update_coordinate_question_id,
)
from app.services.image_loading_service import (
    inspect_image_loading_for_omr,
    load_image_for_omr,
    save_image_for_omr,
)
from app.services.omr_settings_service import EffectiveOmrSettings, get_effective_omr_settings

ACTIVE_PAGE_REVIEW_STATUSES = {"ok", "needs_review"}
RENDER_DPI = 300
REVIEWABLE_RESPONSE_STATUSES = {"blank", "multiple_mark", "unclear", "invalid"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanProcessingSummary:
    total_pages: int
    detected_pages: int
    problem_pages: int
    responses_created: int


@dataclass(frozen=True)
class QrCandidate:
    name: str
    region: str
    rotation: int
    preprocess: str
    image: any
    shape: tuple[int, ...]


def create_scan_batch(db: Session, exam_id: int, filename: str, content: bytes) -> ScanBatch:
    ensure_data_directories()
    upload_dir = get_settings().data_root / "uploaded_scans" / f"exam_{exam_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    target_path = upload_dir / f"{uuid4().hex}_{safe_name}"
    target_path.write_bytes(content)
    batch = ScanBatch(
        exam_id=exam_id,
        uploaded_file_name=filename,
        uploaded_file_path=data_relative_path(target_path),
        status="uploaded",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def batch_has_protected_work(batch: ScanBatch) -> bool:
    """True when the batch holds manual mappings or review corrections that a
    full reprocess (which clears all pages) would silently destroy."""
    for page in batch.scanned_pages:
        if page.qr_status == "manual":
            return True
        for response in page.responses:
            if response.status == "reviewed" or response.reviewed_at is not None:
                return True
    return False


def process_scan_batch(db: Session, batch: ScanBatch, force: bool = False) -> ScanProcessingSummary:
    """(Re)build all pages of a batch from the original upload.

    Destroys every existing page of the batch, including manual mappings and
    review corrections, so it refuses to run on batches with such work unless
    ``force=True`` (the explicit "reprocess from original upload" path).
    """
    if batch.scanned_pages and not force and batch_has_protected_work(batch):
        raise ValueError(
            "This upload contains manual mappings or review corrections. "
            "Use 'Reprocess unmatched pages' instead, or explicitly reprocess "
            "from the original upload to discard that work."
        )
    batch.status = "processing"
    batch.scanned_pages.clear()
    db.commit()
    page_paths = _extract_pages(batch)
    detected_pages = 0
    problem_pages = 0
    responses_created = 0
    for page_no, relative_image_path in enumerate(page_paths, start=1):
        absolute_image_path = get_settings().data_root / relative_image_path
        qr_token = _detect_qr_token(
            absolute_image_path,
            {
                "batch_id": batch.id,
                "exam_id": batch.exam_id,
                "page_no": page_no,
                "image_path": str(absolute_image_path),
            },
        )
        sheet_instance = None
        blank_label = None
        wrong_exam_instance = None
        qr_status = "not_found"
        normalized_qr_token = normalize_qr_token(qr_token) if qr_token else None
        if normalized_qr_token:
            sheet_instance = db.scalar(
                select(SheetInstance)
                .where(SheetInstance.qr_token == normalized_qr_token)
                .where(SheetInstance.exam_id == batch.exam_id)
            )
            if sheet_instance:
                qr_status = "ok"
            else:
                wrong_exam_instance = db.scalar(
                    select(SheetInstance).where(SheetInstance.qr_token == normalized_qr_token)
                )
                if wrong_exam_instance:
                    qr_status = "wrong_exam"
                else:
                    wrong_exam_instance = None
                # Blank extra sheets carry an unassigned token: the session and
                # sheet are known, the student is chosen during upload review.
                blank_label = db.scalar(
                    select(BlankSheetLabel)
                    .where(BlankSheetLabel.qr_token == normalized_qr_token)
                    .where(BlankSheetLabel.exam_id == batch.exam_id)
                )
                if blank_label:
                    qr_status = "blank"
                elif not wrong_exam_instance:
                    qr_status = "invalid"
        if sheet_instance:
            detected_pages += 1
        else:
            problem_pages += 1
        scanned_page = ScannedPage(
            scan_batch_id=batch.id,
            sheet_instance_id=sheet_instance.id if sheet_instance else None,
            student_id=sheet_instance.student_id if sheet_instance else None,
            physical_sheet_id=sheet_instance.physical_sheet_id
            if sheet_instance
            else (blank_label.physical_sheet_id if blank_label else None),
            page_no=page_no,
            image_path=relative_image_path,
            aligned_image_path=relative_image_path,
            qr_token=qr_token or None,
            qr_status=qr_status,
            alignment_status="weak",
            review_status="needs_review" if sheet_instance is None else "ok",
            source_exam_id=wrong_exam_instance.exam_id if wrong_exam_instance else None,
            source_sheet_instance_id=wrong_exam_instance.id if wrong_exam_instance else None,
        )
        db.add(scanned_page)
        db.flush()
        _annotate_qr_debug_match(
            batch_id=batch.id,
            page_no=page_no,
            scanned_page_id=scanned_page.id,
            qr_status=qr_status,
            qr_token=qr_token,
            token_matched=bool(sheet_instance or blank_label),
        )
        if sheet_instance:
            align_scanned_page(scanned_page, sheet_instance)
            responses_created += _read_responses_for_sheet(db, scanned_page, sheet_instance)
            responses_created -= _handle_duplicate_page(db, scanned_page, sheet_instance)
            refresh_page_responses_for_status(db, scanned_page)
            normalize_page_review_status(scanned_page)
    batch.total_pages = len(page_paths)
    batch.detected_pages = detected_pages
    batch.problem_pages = problem_pages
    batch.status = "needs_review" if problem_pages else "processed"
    db.commit()
    return ScanProcessingSummary(
        total_pages=batch.total_pages,
        detected_pages=detected_pages,
        problem_pages=problem_pages,
        responses_created=responses_created,
    )


def reprocess_unmatched_pages(db: Session, batch: ScanBatch) -> ScanProcessingSummary:
    """Re-run QR matching and bubble reading for unmatched pages only.

    Pages already matched to a sheet instance (by QR or manual mapping) are
    never touched, so review corrections and manual work survive.
    """
    responses_created = 0
    for page in batch.scanned_pages:
        if page.sheet_instance_id is not None:
            continue
        image_path = get_settings().data_root / page.image_path
        qr_token = page.qr_token or _detect_qr_token(
            image_path,
            {
                "batch_id": batch.id,
                "exam_id": batch.exam_id,
                "page_no": page.page_no,
                "scanned_page_id": page.id,
                "image_path": str(image_path),
            },
        )
        page.qr_token = qr_token or None
        normalized_qr_token = normalize_qr_token(qr_token) if qr_token else None
        if not normalized_qr_token:
            page.qr_status = "not_found"
            continue
        sheet_instance = db.scalar(
            select(SheetInstance)
            .where(SheetInstance.qr_token == normalized_qr_token)
            .where(SheetInstance.exam_id == batch.exam_id)
        )
        if sheet_instance is None:
            blank_label = db.scalar(
                select(BlankSheetLabel)
                .where(BlankSheetLabel.qr_token == normalized_qr_token)
                .where(BlankSheetLabel.exam_id == batch.exam_id)
            )
            wrong_exam_instance = db.scalar(
                select(SheetInstance).where(SheetInstance.qr_token == normalized_qr_token)
            )
            if wrong_exam_instance:
                page.qr_status = "wrong_exam"
                page.source_exam_id = wrong_exam_instance.exam_id
                page.source_sheet_instance_id = wrong_exam_instance.id
            else:
                page.qr_status = "blank" if blank_label else "invalid"
            if blank_label:
                page.physical_sheet_id = blank_label.physical_sheet_id
            continue
        page.sheet_instance_id = sheet_instance.id
        page.student_id = sheet_instance.student_id
        page.physical_sheet_id = sheet_instance.physical_sheet_id
        page.qr_status = "ok"
        page.review_status = "ok"
        db.query(Response).filter(Response.scanned_page_id == page.id).delete()
        align_scanned_page(page, sheet_instance)
        responses_created += _read_responses_for_sheet(db, page, sheet_instance)
        responses_created -= _handle_duplicate_page(db, page, sheet_instance)
        refresh_page_responses_for_status(db, page)
        normalize_page_review_status(page)

    batch.detected_pages = sum(1 for page in batch.scanned_pages if page.sheet_instance_id)
    batch.problem_pages = sum(1 for page in batch.scanned_pages if not page.sheet_instance_id)
    batch.status = "needs_review" if batch.problem_pages else "processed"
    db.commit()
    return ScanProcessingSummary(
        total_pages=batch.total_pages,
        detected_pages=batch.detected_pages,
        problem_pages=batch.problem_pages,
        responses_created=responses_created,
    )


def _extract_pages(batch: ScanBatch) -> list[str]:
    data_root = get_settings().data_root
    uploaded_path = data_root / batch.uploaded_file_path
    processed_dir = data_root / "processed_pages" / f"exam_{batch.exam_id}" / f"batch_{batch.id}"
    processed_dir.mkdir(parents=True, exist_ok=True)
    suffix = uploaded_path.suffix.lower()
    if suffix == ".pdf":
        return _render_pdf_pages(uploaded_path, processed_dir)
    if suffix in {".jpg", ".jpeg", ".png"}:
        target = processed_dir / uploaded_path.name
        shutil.copyfile(uploaded_path, target)
        return [data_relative_path(target)]
    raise ValueError("Scan batch must be a PDF, JPG, JPEG, or PNG file.")


def _render_pdf_pages(pdf_path: Path, output_dir: Path) -> list[str]:
    rendered_paths: list[str] = []
    document = pdfium.PdfDocument(pdf_path)
    try:
        for index, page in enumerate(document, start=1):
            bitmap = page.render(scale=RENDER_DPI / 72)
            image = bitmap.to_pil()
            output_path = output_dir / f"page_{index:04}.png"
            image.save(output_path)
            rendered_paths.append(data_relative_path(output_path))
    finally:
        document.close()
    return rendered_paths


def _detect_qr_token(image_path: Path, debug_context: dict | None = None) -> str | None:
    load_result = inspect_image_loading_for_omr(image_path, context="qr_detection")
    image = load_result.image
    debug_payload = {
        **(debug_context or {}),
        "image_path": str(image_path),
        "image_loading": load_result.diagnostics.as_dict(),
        "image_shape": list(image.shape) if image is not None else None,
        "candidate_count": 0,
        "candidate_attempts": [],
        "final_token_found": False,
        "final_token_preview": None,
        "final_reason": None,
    }
    if image is None:
        debug_payload["final_reason"] = "QR image could not be loaded."
        _write_qr_debug_payload(debug_payload)
        return None
    detector = cv2.QRCodeDetector()
    candidates = _qr_detection_candidates(image)
    debug_payload["candidate_count"] = len(candidates)
    for candidate in candidates:
        token, points = _try_detect_and_decode(detector, candidate.image)
        _record_qr_attempt(debug_payload, candidate, "detectAndDecode", token, points)
        if token:
            logger.info(
                "QR token decoded",
                extra={
                    "qr_detection": {
                        "candidate": candidate.name,
                        "method": "detectAndDecode",
                        "token_preview": _token_preview(token),
                    }
                },
            )
            debug_payload["final_token_found"] = True
            debug_payload["final_token_preview"] = _token_preview(token)
            debug_payload["final_token_repr"] = repr(token)
            debug_payload["final_reason"] = "QR token decoded."
            _write_qr_debug_payload(debug_payload)
            return token
        multi_token, multi_points = _try_detect_and_decode_multi(detector, candidate.image)
        _record_qr_attempt(
            debug_payload,
            candidate,
            "detectAndDecodeMulti",
            multi_token,
            multi_points,
        )
        if multi_token:
            logger.info(
                "QR token decoded",
                extra={
                    "qr_detection": {
                        "candidate": candidate.name,
                        "method": "detectAndDecodeMulti",
                        "token_preview": _token_preview(multi_token),
                    }
                },
            )
            debug_payload["final_token_found"] = True
            debug_payload["final_token_preview"] = _token_preview(multi_token)
            debug_payload["final_token_repr"] = repr(multi_token)
            debug_payload["final_reason"] = "QR token decoded."
            _write_qr_debug_payload(debug_payload)
            return multi_token
    debug_payload["final_reason"] = _qr_failure_reason(debug_payload["candidate_attempts"])
    _save_qr_candidate_crops(debug_context or {}, candidates, debug_payload)
    _write_qr_debug_payload(debug_payload)
    return None


def _qr_detection_candidates(image) -> list[QrCandidate]:
    height, width = image.shape[:2]
    region_specs = (
        ("full_image", 0.00, 0.00, 1.00, 1.00),
        ("top_band", 0.00, 0.00, 1.00, 0.32),
        ("right_band", 0.58, 0.00, 1.00, 1.00),
        ("upper_right_large", 0.62, 0.00, 1.00, 0.38),
        ("upper_right_medium", 0.70, 0.00, 1.00, 0.32),
        ("upper_right_tight", 0.76, 0.02, 0.99, 0.28),
        ("upper_left", 0.00, 0.00, 0.38, 0.34),
        ("lower_right", 0.62, 0.62, 1.00, 1.00),
        ("lower_left", 0.00, 0.62, 0.38, 1.00),
        ("center_right", 0.58, 0.28, 1.00, 0.72),
    )
    crops = []
    for region, x_min, y_min, x_max, y_max in region_specs:
        crop = image[
            int(height * y_min) : int(height * y_max),
            int(width * x_min) : int(width * x_max),
        ]
        if crop.size:
            crops.append((region, crop))

    candidates = []
    for region, crop in crops:
        for rotation, rotated in _qr_rotations(crop):
            for preprocess, processed in _qr_preprocess_variants(rotated):
                name = f"{region}_rot{rotation}_{preprocess}"
                candidates.append(
                    QrCandidate(
                        name=name,
                        region=region,
                        rotation=rotation,
                        preprocess=preprocess,
                        image=processed,
                        shape=tuple(processed.shape),
                    )
                )
    return candidates


def _qr_rotations(image) -> list[tuple[int, any]]:
    return [
        (0, image),
        (90, cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)),
        (180, cv2.rotate(image, cv2.ROTATE_180)),
        (270, cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)),
    ]


def _qr_preprocess_variants(image) -> list[tuple[str, any]]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    variants = [
        ("original_color", image),
        ("grayscale", gray),
        ("gray_2x", cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)),
        ("gray_3x", cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)),
    ]
    _ignored, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(("otsu_threshold", otsu))
    adaptive = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        5,
    )
    variants.append(("adaptive_threshold", adaptive))
    blurred = cv2.GaussianBlur(gray, (0, 0), 1.0)
    sharpened = cv2.addWeighted(gray, 1.45, blurred, -0.45, 0)
    variants.append(("light_sharpen_gray", sharpened))
    return variants


def _try_detect_and_decode(detector, image) -> tuple[str, any]:
    try:
        token, points, _straight_qrcode = detector.detectAndDecode(image)
        return token or "", points
    except Exception as exc:
        logger.exception("QR detectAndDecode failed")
        return "", {"error": str(exc)}


def _try_detect_and_decode_multi(detector, image) -> tuple[str, any]:
    if not hasattr(detector, "detectAndDecodeMulti"):
        return "", None
    try:
        result = detector.detectAndDecodeMulti(image)
    except Exception as exc:
        logger.exception("QR detectAndDecodeMulti failed")
        return "", {"error": str(exc)}
    ok = bool(result[0]) if result else False
    decoded = result[1] if len(result) > 1 else ()
    points = result[2] if len(result) > 2 else None
    if ok and decoded:
        for token in decoded:
            if token:
                return token, points
    return "", points


def _record_qr_attempt(
    payload: dict,
    candidate: QrCandidate,
    method: str,
    token: str,
    points: any,
) -> None:
    error = points.get("error") if isinstance(points, dict) else None
    points_detected = points is not None and not isinstance(points, dict)
    payload["candidate_attempts"].append(
        {
            "candidate_name": candidate.name,
            "region": candidate.region,
            "rotation": candidate.rotation,
            "preprocess": candidate.preprocess,
            "shape": list(candidate.shape),
            "method": method,
            "points_detected": bool(points_detected),
            "token_found": bool(token),
            "token_preview": _token_preview(token) if token else None,
            "error": error,
        }
    )


def _qr_failure_reason(attempts: list[dict]) -> str:
    if any(attempt.get("points_detected") for attempt in attempts):
        return "QR code points were detected, but decoding failed."
    return "QR candidates were generated, but no QR code was detected."


def _token_preview(token: str | None) -> str | None:
    if not token:
        return None
    if len(token) <= 12:
        return token
    return f"{token[:6]}...{token[-4:]}"


def normalize_qr_token(token: str | None) -> str | None:
    if token is None:
        return None
    return token.strip()


def _scan_debug_enabled() -> bool:
    env_value = os.getenv("ENABLE_SCAN_DEBUG")
    if env_value is not None:
        return env_value == "1"
    return get_settings().enable_scan_debug


def _qr_debug_path(batch_id: int | None, page_no: int | None) -> Path | None:
    if not _scan_debug_enabled() or batch_id is None or page_no is None:
        return None
    return (
        get_settings().data_root
        / "debug"
        / "qr_attempts"
        / f"batch_{batch_id}"
        / f"page_{page_no}_qr_debug.json"
    )


def _write_qr_debug_payload(payload: dict) -> None:
    path = _qr_debug_path(payload.get("batch_id"), payload.get("page_no"))
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _annotate_qr_debug_match(
    batch_id: int,
    page_no: int,
    scanned_page_id: int,
    qr_status: str,
    qr_token: str | None,
    token_matched: bool,
) -> None:
    path = _qr_debug_path(batch_id, page_no)
    if path is None or not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not read QR debug sidecar for match annotation")
        return
    payload["scanned_page_id"] = scanned_page_id
    payload["final_qr_status"] = qr_status
    payload["token_matching"] = {
        "token_decoded": bool(qr_token),
        "token_matched": token_matched,
        "qr_status": qr_status,
        "token_preview": _token_preview(qr_token),
    }
    if qr_status == "ok":
        payload["final_reason"] = "QR token matched a sheet instance."
    elif qr_status == "invalid" and qr_token:
        payload["final_reason"] = (
            "QR token was decoded but did not match any known sheet instance or blank label."
        )
    elif qr_token:
        payload["final_reason"] = (
            "QR token was decoded but did not match a sheet instance for this exam."
        )
    _write_qr_debug_payload(payload)


def _save_qr_candidate_crops(
    debug_context: dict,
    candidates: list[QrCandidate],
    payload: dict,
) -> None:
    if not _scan_debug_enabled():
        return
    batch_id = debug_context.get("batch_id")
    page_no = debug_context.get("page_no")
    if batch_id is None or page_no is None:
        return
    target_dir = (
        get_settings().data_root
        / "debug"
        / "qr_candidates"
        / f"batch_{batch_id}"
        / f"page_{page_no}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    selected = [
        c
        for c in candidates
        if (
            (c.rotation == 0 and c.preprocess == "original_color")
            or (
                c.region.startswith("upper_right")
                and c.preprocess in {"gray_2x", "otsu_threshold", "adaptive_threshold"}
            )
            or (c.region == "full_image" and c.rotation == 0 and c.preprocess == "gray_2x")
        )
    ][:80]
    manifest = []
    for index, candidate in enumerate(selected, start=1):
        filename = f"{index:03}_{candidate.name}.png"
        saved = save_image_for_omr(
            target_dir / filename, candidate.image, context="qr_candidate_debug"
        )
        manifest.append(
            {
                "filename": filename,
                "saved": saved,
                "candidate_name": candidate.name,
                "region": candidate.region,
                "rotation": candidate.rotation,
                "preprocess": candidate.preprocess,
                "shape": list(candidate.shape),
            }
        )
    (target_dir / "manifest.json").write_text(
        json.dumps({"candidates": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    payload["candidate_crops_dir"] = str(target_dir)
    payload["candidate_crops_count"] = len(manifest)


def align_scanned_page(scanned_page: ScannedPage, sheet_instance: SheetInstance) -> None:
    """Warp the scanned page into canonical page space using corner markers.

    Updates ``aligned_image_path`` and ``alignment_status`` on the page (no
    commit). On failure the page keeps the original image and the naive
    coordinate mapping still applies.
    """
    data_root = get_settings().data_root
    source_path = data_root / scanned_page.image_path
    image = load_image_for_omr(source_path, context="alignment")
    if image is None:
        scanned_page.alignment_status = "failed"
        return

    template = sheet_instance.sheet_template
    coordinate_map = template.coordinate_map_json or {}
    marker_positions = (
        template.marker_positions_json or coordinate_map.get("marker_positions") or {}
    )
    page_width_mm, page_height_mm = _page_dimensions_mm(coordinate_map, template.paper_size)

    result = align_page(image, marker_positions, page_width_mm, page_height_mm)
    if result.status == "failed":
        scanned_page.alignment_status = "failed"
        scanned_page.aligned_image_path = scanned_page.image_path
        return

    aligned_path = source_path.with_name(f"{source_path.stem}_aligned.png")
    saved = save_image_for_omr(aligned_path, result.image, context="alignment")
    if not saved:
        scanned_page.alignment_status = "failed"
        scanned_page.aligned_image_path = scanned_page.image_path
        scanned_page.review_status = "needs_review"
        logger.error(
            "Alignment succeeded but aligned image could not be saved",
            extra={
                "omr_alignment": {
                    "scanned_page_id": scanned_page.id,
                    "sheet_instance_id": sheet_instance.id,
                    "source_path": str(source_path),
                    "aligned_path": str(aligned_path),
                    "alignment_result_status": result.status,
                }
            },
        )
        return
    scanned_page.aligned_image_path = data_relative_path(aligned_path)
    scanned_page.alignment_status = result.status


def _page_dimensions_mm(coordinate_map: dict, paper_size: str | None) -> tuple[float, float]:
    width = coordinate_map.get("page_width_mm")
    height = coordinate_map.get("page_height_mm")
    if width and height:
        return float(width), float(height)
    if coordinate_map.get("orientation") == "landscape" or paper_size == "A4-landscape":
        return 297.0, 210.0
    return 210.0, 297.0


def _read_responses_for_sheet(
    db: Session,
    scanned_page: ScannedPage,
    sheet_instance: SheetInstance,
) -> int:
    data_root = get_settings().data_root
    requested_relative_path = scanned_page.aligned_image_path or scanned_page.image_path
    requested_image_path = data_root / requested_relative_path
    original_relative_path = scanned_page.image_path
    original_image_path = data_root / original_relative_path
    relative_path = requested_relative_path
    image_path = requested_image_path
    used_fallback_original = False

    if scanned_page.aligned_image_path and not requested_image_path.exists():
        logger.warning(
            "Aligned scan image is missing; falling back to original image",
            extra={
                "omr_response_reading": {
                    "scanned_page_id": scanned_page.id,
                    "aligned_image_path": str(requested_image_path),
                    "original_image_path": str(original_image_path),
                }
            },
        )
        relative_path = original_relative_path
        image_path = original_image_path
        used_fallback_original = True

    image = load_image_for_omr(
        image_path,
        cv2.IMREAD_GRAYSCALE,
        context="response_reading",
    )
    if image is None and scanned_page.aligned_image_path and image_path != original_image_path:
        logger.warning(
            "Aligned scan image could not be loaded; falling back to original image",
            extra={
                "omr_response_reading": {
                    "scanned_page_id": scanned_page.id,
                    "aligned_image_path": str(image_path),
                    "original_image_path": str(original_image_path),
                }
            },
        )
        relative_path = original_relative_path
        image_path = original_image_path
        used_fallback_original = True
        image = load_image_for_omr(
            image_path,
            cv2.IMREAD_GRAYSCALE,
            context="response_reading_original_fallback",
        )
    sheet_template = sheet_instance.sheet_template
    coordinate_map = sheet_template.coordinate_map_json or {}
    questions = coordinate_map.get("questions") or []
    diagnostic_context = {
        "scanned_page_id": scanned_page.id,
        "sheet_instance_id": sheet_instance.id,
        "sheet_template_id": sheet_template.id,
        "sheet_template_name": getattr(sheet_template.physical_sheet, "sheet_name", None),
        "requested_relative_path": requested_relative_path,
        "requested_image_path": str(requested_image_path),
        "relative_path": relative_path,
        "image_path": str(image_path),
        "image_exists": image_path.exists(),
        "image_loaded": image is not None,
        "image_shape": tuple(image.shape) if image is not None else None,
        "image_dtype": str(image.dtype) if image is not None else None,
        "used_fallback_original": used_fallback_original,
        "coordinate_map_exists": bool(coordinate_map),
        "coordinate_map_keys": sorted(coordinate_map.keys()),
        "questions_count": len(questions),
    }
    if image is None:
        scanned_page.review_status = "needs_review"
        logger.error(
            "Could not read scan image for OMR response reading",
            extra={"omr_response_reading": diagnostic_context},
        )
        return 0
    height, width = image.shape[:2]
    created = 0
    settings = get_effective_omr_settings(db)

    if not questions:
        scanned_page.review_status = "needs_review"
        logger.error(
            "Sheet template has no readable OMR coordinates",
            extra={"omr_response_reading": diagnostic_context},
        )
        return 0

    for question in questions:
        resolved_question = resolve_coordinate_question(db, sheet_instance, question)
        if resolved_question is not None and resolved_question.id != question.get("question_id"):
            update_coordinate_question_id(sheet_template, question, resolved_question.id)
        response_question_id = (
            resolved_question.id if resolved_question is not None else question["question_id"]
        )
        fills = {
            choice["choice"]: _measure_fill(image, width, height, choice, coordinate_map, settings)
            for choice in question["choices"]
        }
        classification = classify_bubble_fills(fills, settings=settings)
        db.add(
            Response(
                scanned_page_id=scanned_page.id,
                question_id=response_question_id,
                detected_answer=classification.detected_answer,
                final_answer=classification.detected_answer
                if classification.status == "ok"
                else None,
                confidence=classification.confidence,
                fill_values_json=fills,
                status=classification.status,
            )
        )
        if classification.status != "ok":
            scanned_page.review_status = "needs_review"
        created += 1
    if created == 0 and questions:
        scanned_page.review_status = "needs_review"
        logger.warning(
            "OMR response reading created 0 responses despite existing question coordinates",
            extra={
                "omr_response_reading": {
                    **diagnostic_context,
                    "responses_created": created,
                }
            },
        )
    else:
        logger.info(
            "OMR response reading complete",
            extra={
                "omr_response_reading": {
                    **diagnostic_context,
                    "responses_created": created,
                }
            },
        )
    return created


def normalize_page_review_status(scanned_page: ScannedPage) -> str:
    """Keep page-level review status aligned with real page/response state."""
    if scanned_page.review_status not in ACTIVE_PAGE_REVIEW_STATUSES:
        return scanned_page.review_status
    if scanned_page.sheet_instance_id is None:
        scanned_page.review_status = "needs_review"
        return "missing_sheet_instance"
    if scanned_page.alignment_status == "failed":
        scanned_page.review_status = "needs_review"
        return "alignment_failed"
    if not scanned_page.responses:
        scanned_page.review_status = "needs_review"
        return "no_responses"
    if any(response.status in REVIEWABLE_RESPONSE_STATUSES for response in scanned_page.responses):
        scanned_page.review_status = "needs_review"
        return "reviewable_responses"
    scanned_page.review_status = "ok"
    return "all_responses_ok"


def refresh_page_responses_for_status(db: Session, scanned_page: ScannedPage) -> None:
    """Reload response rows before deriving page status from the relationship."""
    db.flush()
    db.expire(scanned_page, ["responses"])
    list(scanned_page.responses)


# Bubbles are 2.2 mm radius with 9.4 mm between choice centers, so a +/-1.5 mm
# search can absorb residual local drift without reaching a neighboring bubble.
def _measure_fill(
    image,
    width: int,
    height: int,
    choice: dict,
    coordinate_map: dict | None = None,
    settings: EffectiveOmrSettings | None = None,
) -> float:
    coordinate_map = coordinate_map or {}
    settings = settings or get_effective_omr_settings()
    page_width_mm = coordinate_map.get("page_width_mm", 210)
    page_height_mm = coordinate_map.get("page_height_mm", 297)
    radius_px = max(2, int(choice["radius_mm"] / page_width_mm * width))
    best = 0.0
    drift = settings.local_drift_search_mm
    offsets = (-drift, -drift / 2, 0.0, drift / 2, drift) if drift else (0.0,)
    for dx_mm in offsets:
        for dy_mm in offsets:
            x_px = int((choice["x_mm"] + dx_mm) / page_width_mm * width)
            y_px = int((choice["y_mm"] + dy_mm) / page_height_mm * height)
            best = max(
                best,
                _fill_ratio_at(
                    image, width, height, x_px, y_px, radius_px, settings.dark_pixel_threshold
                ),
            )
    return best


def _fill_ratio_at(
    image, width: int, height: int, x_px: int, y_px: int, radius_px: int, dark_threshold: int = 140
) -> float:
    x_min = max(0, x_px - radius_px)
    x_max = min(width, x_px + radius_px)
    y_min = max(0, y_px - radius_px)
    y_max = min(height, y_px + radius_px)
    crop = image[y_min:y_max, x_min:x_max]
    if crop.size == 0:
        return 0.0
    dark_pixels = (crop < dark_threshold).sum()
    return float(dark_pixels / crop.size)


def _handle_duplicate_page(
    db: Session,
    scanned_page: ScannedPage,
    sheet_instance: SheetInstance,
) -> int:
    existing = db.scalar(
        select(ScannedPage)
        .where(ScannedPage.id != scanned_page.id)
        .where(ScannedPage.sheet_instance_id == sheet_instance.id)
        .where(ScannedPage.student_id == sheet_instance.student_id)
        .where(ScannedPage.physical_sheet_id == sheet_instance.physical_sheet_id)
        .where(ScannedPage.review_status.in_(ACTIVE_PAGE_REVIEW_STATUSES))
        .order_by(ScannedPage.created_at.desc())
    )
    if existing is None:
        return 0
    scanned_page.duplicate_of_page_id = existing.id
    if _can_auto_replace(existing, scanned_page):
        existing.review_status = "replaced"
        existing.replaced_by_page_id = scanned_page.id
        scanned_page.review_status = "ok"
        scanned_page.qr_status = "ok"
        scanned_page.mapping_note = f"Auto-replaced scan page {existing.id}."
        return 0
    scanned_page.review_status = "duplicate_pending"
    scanned_page.qr_status = "duplicate_pending"
    scanned_page.mapping_note = f"Duplicate of accepted scan page {existing.id}."
    db.flush()
    removed = db.query(Response).filter(Response.scanned_page_id == scanned_page.id).count()
    db.query(Response).filter(Response.scanned_page_id == scanned_page.id).delete()
    return removed


def _can_auto_replace(existing: ScannedPage, new_page: ScannedPage) -> bool:
    if existing.qr_status == "manual" or new_page.alignment_status == "failed":
        return False
    return not any(
        response.status == "reviewed" or response.reviewed_at for response in existing.responses
    )


def _safe_filename(filename: str) -> str:
    name = Path(filename or "scan").name
    return "".join(char if char.isalnum() or char in ".-_" else "_" for char in name)
