"""Archive a test session/run: purge its scan files but keep result/insight data.

Archiving is destructive for scans on disk (uploaded PDFs/images and processed page
images are deleted) but never touches the rows that hold results or insights. Path
columns that are declared non-null keep a sentinel (:data:`PURGED_SCAN_PATH`) instead of
NULL so we never have to migrate the column; nullable path columns are set to ``None``.
Image/file routes refuse to serve the sentinel.

Templates archive separately via ``test_template_service.archive_template`` (no scans).
"""

from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    MockExam,
    PeerReviewBatch,
    PeerReviewForm,
    PeerReviewSubmissionPage,
)

# Stored in non-null scan path columns once the underlying file has been deleted.
PURGED_SCAN_PATH = "archived://removed"


def is_purged_scan_path(relative_path: str | None) -> bool:
    return relative_path == PURGED_SCAN_PATH


def _delete_under_root(data_root: Path, relative_path: str | None) -> None:
    """Delete a data-relative file, but only if it really lives under ``data_root``."""
    if not relative_path or relative_path == PURGED_SCAN_PATH:
        return
    path = (data_root / relative_path).resolve()
    if data_root in path.parents and path.is_file():
        path.unlink(missing_ok=True)


def purge_exam_scan_files(db: Session, exam: MockExam) -> None:
    """Remove uploaded/processed scan files for a normal (non-peer-review) session.

    Keeps ScanBatch, ScannedPage, Response, and StudentResult rows so results/insights
    still render; only the on-disk files are deleted and path columns neutralised.
    """
    data_root = get_settings().data_root.resolve()
    for batch in exam.scan_batches:
        for page in batch.scanned_pages:
            _delete_under_root(data_root, page.image_path)
            _delete_under_root(data_root, page.aligned_image_path)
            page.image_path = PURGED_SCAN_PATH
            page.aligned_image_path = None
        _delete_under_root(data_root, batch.uploaded_file_path)
        batch.uploaded_file_path = PURGED_SCAN_PATH


def purge_peer_review_scan_files(db: Session, exam: MockExam) -> None:
    """Remove uploaded/processed scan files for a peer-review session.

    Keeps PeerReviewBatch, PeerReviewSubmissionPage, PeerReviewRating, and the
    final/group result rows so insights still render.
    """
    data_root = get_settings().data_root.resolve()
    form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
    if form is None:
        return
    batches = list(db.scalars(select(PeerReviewBatch).where(PeerReviewBatch.form_id == form.id)))
    for batch in batches:
        pages = list(
            db.scalars(
                select(PeerReviewSubmissionPage).where(
                    PeerReviewSubmissionPage.batch_id == batch.id
                )
            )
        )
        for page in pages:
            _delete_under_root(data_root, page.image_path)
            _delete_under_root(data_root, page.aligned_image_path)
            page.image_path = PURGED_SCAN_PATH
            page.aligned_image_path = None
        _delete_under_root(data_root, batch.uploaded_file_path)
        batch.uploaded_file_path = PURGED_SCAN_PATH


def archive_exam(db: Session, exam: MockExam) -> MockExam:
    """Archive a session/run: remember its status, purge scans, keep results.

    Idempotent: an already-archived session is returned unchanged. Templates are not
    handled here (see ``test_template_service.archive_template``).
    """
    if exam.is_template:
        raise ValueError("Templates archive via archive_template, not archive_exam.")
    if exam.status == "archived":
        return exam
    exam.archived_previous_status = exam.status
    exam.status = "archived"
    exam.archived_at = datetime.utcnow()
    if exam.assessment_type == "peer_review":
        purge_peer_review_scan_files(db, exam)
    else:
        purge_exam_scan_files(db, exam)
    exam.scan_files_purged_at = datetime.utcnow()
    db.commit()
    db.refresh(exam)
    return exam
