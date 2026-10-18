import logging
from collections import Counter
from dataclasses import dataclass, field, replace

import cv2
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import data_relative_path
from app.models import (
    PeerReviewBatch,
    PeerReviewPage,
    PeerReviewRaterGroup,
    PeerReviewRating,
    PeerReviewSubmissionPage,
)
from app.services.alignment_service import align_page
from app.services.bubble_reader_service import classify_bubble_fills
from app.services.omr_settings_service import EffectiveOmrSettings, get_effective_omr_settings
from app.services.scan_batch_service import _detect_qr_token, _extract_pages, _measure_fill

logger = logging.getLogger(__name__)

# Fraction of the bubble radius actually sampled. Measuring the inner disc keeps the
# printed (light) circle outline and any table border out of the fill ratio.
PEER_FILL_RADIUS_RATIO = 0.78


def peer_review_omr_settings(base: EffectiveOmrSettings) -> EffectiveOmrSettings:
    """Light-pencil-tolerant settings for peer-review sheets.

    Hand-drawn rating marks are often fainter than exam bubbles, so we count slightly
    lighter pixels as dark and lower the mark/weak thresholds — while staying below the
    grey level the bubble outline and the printed score digit are drawn in, so neither
    is ever read as a mark. The global OMR settings for normal tests are untouched.
    """
    return replace(
        base,
        dark_pixel_threshold=max(base.dark_pixel_threshold, 160),
        mark_threshold=min(base.mark_threshold, 0.30),
        weak_threshold=min(base.weak_threshold, 0.18),
        min_gap_between_top_two=min(base.min_gap_between_top_two, 0.10),
        local_drift_search_mm=max(base.local_drift_search_mm, 1.2),
    )


@dataclass(frozen=True)
class PeerReviewScanProcessingSummary:
    total_pages: int
    detected_pages: int
    problem_pages: int
    ratings_created: int
    # Per-status breakdown of the ratings created this run, for real-scan diagnostics.
    # Keys: ok, blank, multiple_mark, unclear, invalid (any classifier status).
    status_counts: dict[str, int] = field(default_factory=dict)

    @property
    def blank_count(self) -> int:
        return self.status_counts.get("blank", 0)

    @property
    def multiple_mark_count(self) -> int:
        return self.status_counts.get("multiple_mark", 0)

    @property
    def unclear_count(self) -> int:
        return self.status_counts.get("unclear", 0)

    @property
    def invalid_count(self) -> int:
        return self.status_counts.get("invalid", 0)


def create_peer_review_batch(
    db: Session,
    form_id: int,
    rater_group_id: int,
    uploaded_file_name: str,
    uploaded_file_path: str,
) -> PeerReviewBatch:
    group = db.get(PeerReviewRaterGroup, rater_group_id)
    if group is None or group.form_id != form_id:
        raise ValueError("Rater group does not belong to this peer-review form.")
    batch = PeerReviewBatch(
        form_id=form_id,
        rater_group_id=rater_group_id,
        uploaded_file_name=uploaded_file_name,
        uploaded_file_path=uploaded_file_path,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def record_submission_page(
    db: Session,
    batch: PeerReviewBatch,
    page: PeerReviewPage,
    page_no_in_upload: int,
    image_path: str,
    detected_scores: dict[tuple[int, int], int | None],
    confidences: dict[tuple[int, int], float] | None = None,
) -> PeerReviewSubmissionPage:
    submission = PeerReviewSubmissionPage(
        batch_id=batch.id,
        peer_review_page_id=page.id,
        page_no_in_upload=page_no_in_upload,
        image_path=image_path,
    )
    db.add(submission)
    db.flush()
    confidences = confidences or {}
    form = page.form
    seen = set()
    for bubble in page.coordinate_map_json.get("rating_bubbles", []):
        key = (bubble["target_student_id"], bubble["competency_id"])
        if key in seen:
            continue
        seen.add(key)
        score = detected_scores.get(key)
        status = "blank" if score is None else "ok"
        db.add(
            PeerReviewRating(
                exam_id=form.exam_id,
                form_id=form.id,
                batch_id=batch.id,
                rater_group_id=batch.rater_group_id,
                submission_page_id=submission.id,
                peer_review_page_id=page.id,
                target_student_id=key[0],
                competency_id=key[1],
                detected_score=score,
                final_score=score,
                confidence=confidences.get(key),
                status=status,
            )
        )
    batch.total_pages += 1
    batch.detected_pages += 1
    db.commit()
    db.refresh(submission)
    return submission


def process_peer_review_batch(
    db: Session,
    batch: PeerReviewBatch,
    force: bool = False,
) -> PeerReviewScanProcessingSummary:
    if batch.form is None:
        raise ValueError("Peer-review batch has no form.")
    if batch.form_id != batch.rater_group.form_id:
        raise ValueError("Peer-review batch rater group does not belong to the form.")
    if batch.status == "processed" and not force:
        return PeerReviewScanProcessingSummary(
            total_pages=batch.total_pages,
            detected_pages=batch.detected_pages,
            problem_pages=batch.problem_pages,
            ratings_created=0,
            status_counts={},
        )

    db.query(PeerReviewRating).filter(PeerReviewRating.batch_id == batch.id).delete()
    db.query(PeerReviewSubmissionPage).filter(
        PeerReviewSubmissionPage.batch_id == batch.id
    ).delete()
    batch.status = "processing"
    db.commit()

    page_paths = _extract_pages_for_peer_review(batch)
    detected_pages = 0
    problem_pages = 0
    ratings_created = 0
    status_counts: Counter[str] = Counter()
    for page_no, relative_image_path in enumerate(page_paths, start=1):
        image_path = get_settings().data_root / relative_image_path
        qr_token = _detect_qr_token(image_path)
        page = None
        qr_status = "not_found"
        if qr_token:
            page = db.scalar(
                select(PeerReviewPage)
                .where(PeerReviewPage.qr_token == qr_token)
                .where(PeerReviewPage.form_id == batch.form_id)
            )
            qr_status = "ok" if page else "invalid"
        if page is None:
            problem_pages += 1
            continue
        detected_pages += 1
        aligned_image_path, alignment_status = _align_peer_review_page(page, relative_image_path)
        if alignment_status == "failed":
            detected_scores, confidences, statuses = _unclear_scores_for_page(page)
        else:
            read_image_path = aligned_image_path or relative_image_path
            detected_scores, confidences, statuses = _read_page_scores(page, read_image_path, db)
        submission = _record_submission_page_with_statuses(
            db,
            batch,
            page,
            page_no,
            relative_image_path,
            aligned_image_path,
            alignment_status,
            detected_scores,
            statuses,
            confidences,
            qr_status,
        )
        if submission.review_status == "needs_review":
            problem_pages += 1
        ratings_created += len(statuses)
        status_counts.update(statuses.values())

    batch.total_pages = len(page_paths)
    batch.detected_pages = detected_pages
    batch.problem_pages = problem_pages
    batch.status = "needs_review" if problem_pages else "processed"
    db.commit()
    summary = PeerReviewScanProcessingSummary(
        total_pages=batch.total_pages,
        detected_pages=batch.detected_pages,
        problem_pages=batch.problem_pages,
        ratings_created=ratings_created,
        status_counts=dict(status_counts),
    )
    # Per-batch diagnostic line for real-scan reliability triage (pencil/pen/photo/etc.).
    logger.info(
        "peer-review batch %s: pages=%s detected=%s problems=%s ratings=%s "
        "ok=%s blank=%s multiple_mark=%s unclear=%s invalid=%s",
        batch.id,
        summary.total_pages,
        summary.detected_pages,
        summary.problem_pages,
        summary.ratings_created,
        status_counts.get("ok", 0),
        summary.blank_count,
        summary.multiple_mark_count,
        summary.unclear_count,
        summary.invalid_count,
    )
    return summary


def _extract_pages_for_peer_review(batch: PeerReviewBatch) -> list[str]:
    class Adapter:
        id = batch.id
        exam_id = f"peer_review_form_{batch.form_id}"
        uploaded_file_path = batch.uploaded_file_path

    return _extract_pages(Adapter())


def _read_page_scores(
    page: PeerReviewPage,
    relative_image_path: str,
    db: Session,
) -> tuple[
    dict[tuple[int, int], int | None],
    dict[tuple[int, int], float | None],
    dict[tuple[int, int], str],
]:
    image_path = get_settings().data_root / relative_image_path
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    coordinate_map = page.coordinate_map_json or {}
    expected_keys = _expected_rating_keys(coordinate_map)
    if image is None:
        return (
            {key: None for key in expected_keys},
            {key: None for key in expected_keys},
            {key: "invalid" for key in expected_keys},
        )
    height, width = image.shape[:2]
    settings = peer_review_omr_settings(get_effective_omr_settings(db))
    by_cell: dict[tuple[int, int], dict[str, float]] = {}
    for bubble in coordinate_map.get("rating_bubbles", []):
        key = (bubble["target_student_id"], bubble["competency_id"])
        by_cell.setdefault(key, {})[str(bubble["score"])] = _measure_peer_fill(
            image,
            width,
            height,
            bubble,
            coordinate_map,
            settings,
        )
    scores = {}
    confidences = {}
    statuses = {}
    for key, fills in by_cell.items():
        classification = classify_bubble_fills(fills, settings=settings)
        scores[key] = (
            int(classification.detected_answer) if classification.detected_answer else None
        )
        confidences[key] = classification.confidence
        statuses[key] = classification.status
    return scores, confidences, statuses


def _expected_rating_keys(coordinate_map: dict) -> set[tuple[int, int]]:
    return {
        (bubble["target_student_id"], bubble["competency_id"])
        for bubble in coordinate_map.get("rating_bubbles", [])
    }


def _unclear_scores_for_page(
    page: PeerReviewPage,
) -> tuple[dict[tuple[int, int], None], dict[tuple[int, int], float], dict[tuple[int, int], str]]:
    expected_keys = _expected_rating_keys(page.coordinate_map_json or {})
    return (
        {key: None for key in expected_keys},
        {key: 0.0 for key in expected_keys},
        {key: "unclear" for key in expected_keys},
    )


def _record_submission_page_with_statuses(
    db: Session,
    batch: PeerReviewBatch,
    page: PeerReviewPage,
    page_no_in_upload: int,
    image_path: str,
    aligned_image_path: str | None,
    alignment_status: str,
    detected_scores: dict[tuple[int, int], int | None],
    statuses: dict[tuple[int, int], str],
    confidences: dict[tuple[int, int], float | None],
    qr_status: str,
) -> PeerReviewSubmissionPage:
    submission = PeerReviewSubmissionPage(
        batch_id=batch.id,
        peer_review_page_id=page.id,
        page_no_in_upload=page_no_in_upload,
        qr_status=qr_status,
        alignment_status=alignment_status,
        review_status="ok",
        image_path=image_path,
        aligned_image_path=aligned_image_path,
    )
    db.add(submission)
    db.flush()
    form = page.form
    for key, status in statuses.items():
        score = detected_scores.get(key)
        if status != "ok":
            submission.review_status = "needs_review"
        db.add(
            PeerReviewRating(
                exam_id=form.exam_id,
                form_id=form.id,
                batch_id=batch.id,
                rater_group_id=batch.rater_group_id,
                submission_page_id=submission.id,
                peer_review_page_id=page.id,
                target_student_id=key[0],
                competency_id=key[1],
                detected_score=score,
                final_score=score if status == "ok" else None,
                confidence=confidences.get(key),
                status=status,
            )
        )
    return submission


def _measure_peer_fill(
    image,
    width: int,
    height: int,
    bubble: dict,
    coordinate_map: dict,
    settings: EffectiveOmrSettings,
) -> float:
    """Fill ratio of a single rating bubble.

    Reuses the normal OMR sampler (``_measure_fill``) so peer-review reading inherits
    the local-drift search and dark-pixel threshold, but samples only the inner disc so
    the printed outline and table borders never dominate the ratio. The peer-review
    coordinate map always uses ``mm_top_left`` bubbles (``x_mm``/``y_mm``/``radius_mm``),
    which is exactly the shape ``_measure_fill`` expects.
    """
    inner = dict(bubble)
    inner["radius_mm"] = float(bubble.get("radius_mm", 1.8)) * PEER_FILL_RADIUS_RATIO
    return _measure_fill(image, width, height, inner, coordinate_map, settings)


def _align_peer_review_page(
    page: PeerReviewPage,
    relative_image_path: str,
) -> tuple[str | None, str]:
    coordinate_map = page.coordinate_map_json or {}
    marker_positions = coordinate_map.get("marker_positions") or {}
    if not marker_positions:
        return None, "not_aligned"
    data_root = get_settings().data_root
    source_path = data_root / relative_image_path
    image = cv2.imread(str(source_path))
    if image is None:
        return None, "failed"
    result = align_page(
        image,
        marker_positions,
        float(coordinate_map.get("page_width_mm", 210)),
        float(coordinate_map.get("page_height_mm", 297)),
    )
    if result.status == "failed":
        return None, "failed"
    aligned_path = source_path.with_name(f"{source_path.stem}_aligned.png")
    cv2.imwrite(str(aligned_path), result.image)
    return data_relative_path(aligned_path), result.status
