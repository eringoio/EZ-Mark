from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    MockExam,
    PeerReviewBatch,
    PeerReviewFinalResult,
    PeerReviewForm,
    PeerReviewGroupResult,
    PeerReviewRating,
    PeerReviewSubmissionPage,
)
from app.services.exam_service import list_exams, next_exam_action
from app.services.i18n_service import _
from app.services.list_filter_service import (
    filter_context,
    month_fallback_filters,
    paginate_items,
    parse_list_filters,
)
from app.services.review_service import count_reviewable
from app.services.templates import templates

# Peer-review state lives in dedicated tables, so a session counts as "scanned" when any
# of these exist — even if a batch's page counter is stale.
PEER_REVIEW_SCAN_STATUSES = (
    "printed",
    "scanning",
    "ready_to_score",
    "needs_review",
    "results_ready",
)


def _peer_review_scanned_page_count(db: Session, form_id: int) -> int:
    """Robust scanned-page count: max of batch totals, submission pages, and rated pages.

    Uses the maximum so the figure is never 0 just because a batch counter is stale.
    """
    batch_total = (
        db.scalar(
            select(func.coalesce(func.sum(PeerReviewBatch.total_pages), 0)).where(
                PeerReviewBatch.form_id == form_id
            )
        )
        or 0
    )
    submission_pages = (
        db.scalar(
            select(func.count(PeerReviewSubmissionPage.id))
            .join(
                PeerReviewBatch, PeerReviewSubmissionPage.batch_id == PeerReviewBatch.id
            )
            .where(PeerReviewBatch.form_id == form_id)
        )
        or 0
    )
    rated_pages = (
        db.scalar(
            select(func.count(func.distinct(PeerReviewRating.submission_page_id))).where(
                PeerReviewRating.form_id == form_id
            )
        )
        or 0
    )
    return max(batch_total, submission_pages, rated_pages)


def _peer_review_has_scan_activity(db: Session, exam) -> bool:
    """True when a peer-review session has any scan/review/result state worth showing."""
    if exam.status in PEER_REVIEW_SCAN_STATUSES:
        return True
    form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
    if form is None:
        return False
    for model in (PeerReviewBatch, PeerReviewRating, PeerReviewFinalResult, PeerReviewGroupResult):
        if db.scalar(select(func.count(model.id)).where(model.form_id == form.id)) or 0:
            return True
    return False


def _session_state(db: Session, exam) -> dict:
    """Workflow state for the Scan & Review and Insights overview cards."""
    review_count = count_reviewable(db, exam.id)
    if exam.assessment_type == "peer_review":
        form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
        uploaded_pages = 0
        batch_count = 0
        rating_count = 0
        has_results = False
        if form:
            uploaded_pages = _peer_review_scanned_page_count(db, form.id)
            batch_count = db.scalar(
                select(func.count(PeerReviewBatch.id)).where(PeerReviewBatch.form_id == form.id)
            ) or 0
            rating_count = db.scalar(
                select(func.count(PeerReviewRating.id)).where(PeerReviewRating.form_id == form.id)
            ) or 0
            has_results = bool(
                db.scalar(select(func.count(PeerReviewFinalResult.id)).where(PeerReviewFinalResult.form_id == form.id))
                or db.scalar(select(func.count(PeerReviewGroupResult.id)).where(PeerReviewGroupResult.form_id == form.id))
            )
        upload_url = f"/exams/{exam.id}/peer-review/scan-batches"
        review_url = f"/exams/{exam.id}/peer-review/review"
        # Peer-review scan/review/result state lives in dedicated tables, so a missing or
        # stale batch counter must never hide ratings, scanned pages, or results. Mirror
        # the workflow-status order (needs_review -> results_ready -> ready_to_score) so the
        # overview card and ``exam.status`` cannot contradict each other.
        if not (batch_count or uploaded_pages or rating_count or has_results):
            state = "no_scans"
        elif review_count > 0:
            state = "needs_review"
        elif has_results:
            state = "results_ready"
        elif rating_count:
            state = "ready_to_score"
        else:
            # Activity exists only as an empty/failed batch (no ratings yet) — send the
            # teacher back to upload rather than offering a score/results action.
            state = "no_scans"
    else:
        uploaded_pages = sum(batch.total_pages for batch in exam.scan_batches)
        batch_count = len(exam.scan_batches)
        has_results = bool(exam.results) or exam.status == "results_ready"
        upload_url = f"/exams/{exam.id}/scan-batches"
        review_url = f"/exams/{exam.id}/review"
        if not batch_count:
            state = "no_scans"
        elif review_count > 0:
            state = "needs_review"
        elif has_results:
            state = "results_ready"
        else:
            state = "not_calculated"
    return {
        "exam": exam,
        "state": state,
        "review_count": review_count,
        "uploaded_pages": uploaded_pages,
        "has_results": has_results,
        "upload_url": upload_url,
        "review_url": review_url,
        "results_url": f"/exams/{exam.id}/results",
        "open_url": upload_url,
        # Archived sessions are read-only: their scans are purged, so the overview cards
        # must not offer upload/review/recalculate — only view/results/export.
        "is_archived": exam.status == "archived",
    }

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    exams = list_exams(db)
    cards = [
        {
            "label": _("dashboard.metrics.active_mock_exams"),
            "value": str(len([e for e in exams if e.status not in ("locked", "archived")])),
        },
        {
            "label": _("dashboard.metrics.needs_review"),
            "value": str(len([e for e in exams if e.status == "needs_review"])),
        },
        {"label": _("dashboard.metrics.recent_exports"), "value": "0"},
        {"label": _("dashboard.metrics.students_imported"), "value": "0"},
    ]
    recent_sessions = [e for e in exams if not e.is_template][:6]
    needs_review = [e for e in exams if e.status == "needs_review"][:5]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "cards": cards,
            "active_nav": "dashboard",
            "exams": exams[:8],
            "recent_sessions": recent_sessions,
            "needs_review": needs_review,
            "next_exam_action": next_exam_action,
        },
    )


def _scan_review_sessions(db: Session, exams: list[MockExam]) -> list:
    """Sessions with scan batches or in a scanning/review state (peer-review sessions store
    scans/ratings in dedicated tables, so they are checked explicitly)."""
    return [
        _session_state(db, e)
        for e in exams
        if (
            e.assessment_type == "peer_review"
            and _peer_review_has_scan_activity(db, e)
        )
        or (
            e.assessment_type != "peer_review"
            and (e.scan_batches or e.status in ("scanning", "needs_review", "printed"))
        )
    ]


@router.get("/scan-review", response_class=HTMLResponse)
def scan_review_overview(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    filters = parse_list_filters(request.query_params)
    # Archived sessions are excluded by the default filter (their scans are purged).
    scan_sessions = _scan_review_sessions(db, list_exams(db, filters))
    paginated = paginate_items(scan_sessions, filters)
    month_fallback_sessions = (
        _scan_review_sessions(db, list_exams(db, month_fallback_filters(filters)))
        if filters.date and not paginated.items
        else []
    )
    return templates.TemplateResponse(
        request,
        "scan_review/index.html",
        {
            "request": request,
            "active_nav": "scan_review",
            "scan_sessions": paginated.items,
            "month_fallback_sessions": month_fallback_sessions,
            "filters": filter_context(filters),
            "filter_action": "/scan-review",
            "pagination": paginated,
        },
    )


def _insights_sessions(db: Session, exams: list[MockExam]) -> list:
    """Sessions with scans or results in any state, so nothing in progress looks lost.
    Peer-review sessions track scans/ratings/results in dedicated tables. Archived sessions
    keep their result/insight data, so when shown (archived=show/only) they still surface here
    even though their scan files were purged."""
    return [
        _session_state(db, e)
        for e in exams
        if (
            e.assessment_type == "peer_review"
            and _peer_review_has_scan_activity(db, e)
        )
        or (
            e.assessment_type != "peer_review"
            and (
                e.scan_batches
                or e.results
                or e.status in ("results_ready", "needs_review")
            )
        )
    ]


@router.get("/insights", response_class=HTMLResponse)
def insights_overview(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    filters = parse_list_filters(request.query_params)
    result_sessions = _insights_sessions(db, list_exams(db, filters))
    paginated = paginate_items(result_sessions, filters)
    month_fallback_sessions = (
        _insights_sessions(db, list_exams(db, month_fallback_filters(filters)))
        if filters.date and not paginated.items
        else []
    )
    return templates.TemplateResponse(
        request,
        "insights/index.html",
        {
            "request": request,
            "active_nav": "insights",
            "result_sessions": paginated.items,
            "month_fallback_sessions": month_fallback_sessions,
            "filters": filter_context(filters),
            "filter_action": "/insights",
            "pagination": paginated,
        },
    )
