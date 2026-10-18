"""Single source of truth for a peer-review exam's workflow status.

Peer-review scan/review/result state lives in dedicated tables (``PeerReviewBatch``,
``PeerReviewRating``, ``PeerReviewFinalResult``/``PeerReviewGroupResult``), not the
normal ``exam.scan_batches``/``exam.results`` relationships. This module derives the
``MockExam.status`` from those tables so the dashboard, exam detail stepper, and
scan-review overview never get stuck on "upload scans" once ratings/results exist.

Call :func:`refresh_peer_review_exam_status` after any route that changes peer-review
scan/review/result state (upload, review correction, recalculate, regenerate).
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    MockExam,
    PeerReviewBatch,
    PeerReviewFinalResult,
    PeerReviewForm,
    PeerReviewGroupResult,
    PeerReviewRating,
)
from app.services.peer_review_review_service import PROBLEM_STATUSES
from app.services.sheet_generation_service import latest_exam_download_path


def peer_review_has_results(db: Session, form_id: int) -> bool:
    final_count = (
        db.scalar(
            select(func.count(PeerReviewFinalResult.id)).where(
                PeerReviewFinalResult.form_id == form_id
            )
        )
        or 0
    )
    if final_count:
        return True
    group_count = (
        db.scalar(
            select(func.count(PeerReviewGroupResult.id)).where(
                PeerReviewGroupResult.form_id == form_id
            )
        )
        or 0
    )
    return group_count > 0


def refresh_peer_review_exam_status(
    db: Session, exam: MockExam, form: PeerReviewForm | None = None
) -> str:
    """Re-derive and persist ``exam.status`` from live peer-review scan/review state.

    Returns the resolved status. Only touches peer-review exams; other assessment types
    are left untouched so normal test status handling is unaffected.
    """
    if exam.assessment_type != "peer_review":
        return exam.status
    if form is None:
        form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
    if form is None:
        return exam.status

    total_ratings = (
        db.scalar(
            select(func.count(PeerReviewRating.id)).where(PeerReviewRating.form_id == form.id)
        )
        or 0
    )
    unresolved = (
        db.scalar(
            select(func.count(PeerReviewRating.id))
            .where(PeerReviewRating.form_id == form.id)
            .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
        )
        or 0
    )
    batch_count = (
        db.scalar(select(func.count(PeerReviewBatch.id)).where(PeerReviewBatch.form_id == form.id))
        or 0
    )

    if peer_review_has_results(db, form.id):
        status = "results_ready"
    elif unresolved:
        status = "needs_review"
    elif total_ratings:
        status = "ready_to_score"
    elif batch_count:
        status = "scanning"
    elif latest_exam_download_path(db, exam) is not None:
        status = "printed"
    elif exam.status == "draft":
        # No scans/sheets yet — leave a freshly created session in draft.
        status = "draft"
    else:
        status = "ready_to_print"

    if exam.status != status:
        exam.status = status
        db.commit()
    return status
