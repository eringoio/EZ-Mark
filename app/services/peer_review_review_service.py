"""Data for the image + overlay peer-review confirmation screen.

Mirrors the normal scan-review experience (scanned page image with an expected-bubble
overlay and a problem queue) but works against the anonymous peer-review model:
``PeerReviewSubmissionPage`` images, ``PeerReviewPage.coordinate_map_json`` bubbles and
``PeerReviewRating`` rows keyed by ``(target_student_id, competency_id)``.
"""

from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Competency,
    PeerReviewForm,
    PeerReviewRating,
    PeerReviewSubmissionPage,
    Student,
)

# Unresolved ratings that keep an item in the review queue.
PROBLEM_STATUSES = ("unclear", "multiple_mark", "blank", "invalid")
# Resolved ratings. ``reviewed`` carries a manual score; ``reviewed_blank`` and
# ``reviewed_invalid`` are manually confirmed non-scoring outcomes that must leave the
# queue (the original ``blank``/``invalid`` are problem states, not resolutions).
RESOLVED_STATUSES = ("ok", "reviewed", "reviewed_blank", "reviewed_invalid")


def list_problem_ratings(db: Session, form: PeerReviewForm) -> list[PeerReviewRating]:
    return list(
        db.scalars(
            select(PeerReviewRating)
            .where(PeerReviewRating.form_id == form.id)
            .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
            .order_by(
                PeerReviewRating.batch_id,
                PeerReviewRating.submission_page_id,
                PeerReviewRating.target_student_id,
                PeerReviewRating.competency_id,
            )
        )
    )


def next_problem_rating_id(
    db: Session, form: PeerReviewForm, after: PeerReviewRating | None
) -> int | None:
    """Id of the next unresolved rating to review after the given one (wraps around)."""
    problems = [rating for rating in list_problem_ratings(db, form)]
    if not problems:
        return None
    if after is None:
        return problems[0].id
    ids = [rating.id for rating in problems]
    if after.id in ids:
        index = ids.index(after.id)
        return ids[(index + 1) % len(ids)]
    return ids[0]


def build_review_context(db: Session, form: PeerReviewForm, rating_id: int | None = None) -> dict:
    """Everything the review template/JS needs for the current submission page."""
    problems = list_problem_ratings(db, form)
    current = next((rating for rating in problems if rating.id == rating_id), None)
    if current is None:
        current = problems[0] if problems else None

    competencies = {
        comp.id: comp
        for comp in db.scalars(select(Competency).where(Competency.exam_id == form.exam_id))
    }
    # Printed-sheet labels ("1. 山田 太郎") keyed by student, taken from the form's targets.
    target_labels = {target.student_id: target.display_label for target in form.targets}
    # Load students referenced by this form's targets *and* by any rating on this form, so a
    # rating whose target was later removed/disabled still resolves to a real student name.
    # Stay scoped to this form — never pull students from other exams/sessions.
    student_ids = set(target_labels)
    student_ids.update(
        db.scalars(
            select(PeerReviewRating.target_student_id).where(PeerReviewRating.form_id == form.id)
        )
    )
    students = {}
    if student_ids:
        students = {
            student.id: student
            for student in db.scalars(select(Student).where(Student.id.in_(student_ids)))
        }
    queue = [
        {
            "id": rating.id,
            "target": _target_display_label(
                target_labels.get(rating.target_student_id),
                students.get(rating.target_student_id),
            ),
            "competency": competencies[rating.competency_id].name
            if rating.competency_id in competencies
            else "—",
            "status": rating.status,
            "detected": rating.detected_score,
            "is_current": current is not None and rating.id == current.id,
        }
        for rating in problems
    ]

    submission = None
    coordinate_map: dict = {}
    rating_map: dict[str, dict] = {}
    image_url = None
    submission_label = None
    if current is not None and current.submission_page_id:
        submission = db.get(PeerReviewSubmissionPage, current.submission_page_id)
    if submission is not None:
        page = submission.peer_review_page
        coordinate_map = page.coordinate_map_json or {}
        image_url = f"/peer-review/submission-pages/{submission.id}/image"
        submission_label = page.title or f"Page {page.page_no}"
        submission_ratings = list(
            db.scalars(
                select(PeerReviewRating).where(PeerReviewRating.submission_page_id == submission.id)
            )
        )
        for rating in submission_ratings:
            key = f"{rating.target_student_id}_{rating.competency_id}"
            rating_map[key] = {
                "rating_id": rating.id,
                "detected": rating.detected_score,
                "final": rating.final_score,
                "status": rating.status,
                "is_current": current is not None and rating.id == current.id,
                "resolved": rating.status in RESOLVED_STATUSES,
            }

    current_target = students.get(current.target_student_id) if current else None
    current_target_label = (
        _target_display_label(target_labels.get(current.target_student_id), current_target)
        if current
        else "—"
    )

    return {
        "current": current,
        "current_competency": competencies.get(current.competency_id) if current else None,
        "current_target": current_target,
        "current_target_label": current_target_label,
        "submission": submission,
        "coordinate_map": coordinate_map,
        "rating_map": rating_map,
        "image_url": image_url,
        "submission_label": submission_label,
        "queue": queue,
        "problem_count": len(problems),
        "bulk_page_remaining_count": count_remaining_submission_ratings(db, submission.id)
        if submission is not None
        else 0,
        "score_min": form.score_min,
        "score_max": form.score_max,
    }


def apply_rating_correction(
    db: Session,
    rating: PeerReviewRating,
    *,
    score: int | None,
    status: str,
    score_min: int,
    score_max: int,
    reviewed_by: str = "staff",
) -> None:
    """Persist a manual correction and re-derive the submission's review status.

    A valid manual score resolves to ``reviewed`` (scoring); a confirmed blank/invalid
    resolves to the non-scoring ``reviewed_blank``/``reviewed_invalid``. All three are
    resolved statuses, so the item always leaves the problem queue.
    """
    if score is not None and score_min <= score <= score_max:
        rating.status = "reviewed"
        rating.final_score = score
    elif status == "blank":
        rating.status = "reviewed_blank"
        rating.final_score = None
    else:
        # Anything else (explicit "invalid", or an out-of-range/missing score) is a
        # confirmed non-scoring rating.
        rating.status = "reviewed_invalid"
        rating.final_score = None
    rating.reviewed_by = reviewed_by
    rating.reviewed_at = datetime.utcnow()
    db.flush()
    _refresh_submission_status(db, rating.submission_page_id)
    db.commit()


def count_remaining_submission_ratings(db: Session, submission_page_id: int) -> int:
    return db.scalar(
        select(func.count(PeerReviewRating.id))
        .where(PeerReviewRating.submission_page_id == submission_page_id)
        .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
    ) or 0


def resolve_remaining_submission_ratings(
    db: Session,
    submission_page_id: int,
    action: Literal["blank", "invalid"],
    reviewed_by: str = "staff",
) -> int:
    if action not in {"blank", "invalid"}:
        raise ValueError("Unsupported bulk review action.")
    submission = db.get(PeerReviewSubmissionPage, submission_page_id)
    if submission is None:
        raise ValueError("Peer-review submission page not found.")

    ratings = list(
        db.scalars(
            select(PeerReviewRating)
            .where(PeerReviewRating.submission_page_id == submission_page_id)
            .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
        )
    )
    reviewed_at = datetime.utcnow()
    resolved_status = "reviewed_blank" if action == "blank" else "reviewed_invalid"
    for rating in ratings:
        rating.status = resolved_status
        rating.final_score = None
        rating.reviewed_by = reviewed_by
        rating.reviewed_at = reviewed_at

    db.flush()
    _refresh_submission_status(db, submission_page_id)
    db.commit()
    return len(ratings)


def _refresh_submission_status(db: Session, submission_page_id: int | None) -> None:
    if not submission_page_id:
        return
    submission = db.get(PeerReviewSubmissionPage, submission_page_id)
    if submission is None:
        return
    remaining = db.scalar(
        select(PeerReviewRating)
        .where(PeerReviewRating.submission_page_id == submission_page_id)
        .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
        .limit(1)
    )
    submission.review_status = "needs_review" if remaining else "ok"
    batch = submission.batch
    if batch is not None:
        batch_remaining = db.scalar(
            select(PeerReviewRating)
            .where(PeerReviewRating.batch_id == batch.id)
            .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
            .limit(1)
        )
        batch.problem_pages = _count_problem_pages(db, batch.id)
        batch.status = "needs_review" if batch_remaining else "processed"


def _count_problem_pages(db: Session, batch_id: int) -> int:
    pages = db.scalars(
        select(PeerReviewSubmissionPage).where(PeerReviewSubmissionPage.batch_id == batch_id)
    )
    return sum(1 for page in pages if page.review_status == "needs_review")


def _target_display_label(label: str | None, student: Student | None) -> str:
    """Review-queue/current-item label, matching the printed peer-review sheet.

    Prefer the target's printed ``display_label`` ("1. 山田 太郎"). Fall back safely when
    the target is gone (an old rating): student name, then student number + name, then a
    dash when there is no data at all.
    """
    if label and label.strip():
        return label.strip()
    if student is None:
        return "—"
    if student.name and student.name.strip():
        return student.name.strip()
    return f"{student.student_number} {student.name}".strip() or "—"
