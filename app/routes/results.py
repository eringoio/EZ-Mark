from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PeerReviewForm
from app.services.exam_service import get_run_or_404
from app.services.peer_review_insights_service import build_peer_review_insights
from app.services.peer_review_scoring_service import recalculate_anonymous_peer_review_results
from app.services.peer_review_workflow_service import refresh_peer_review_exam_status
from app.services.scoring_service import (
    question_analysis,
    recalculate_exam_results,
    summarize_results,
)
from app.services.templates import templates

router = APIRouter()


@router.get("/exams/{exam_id}/results", response_class=HTMLResponse)
def results_page(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id)

    if exam.assessment_type == "peer_review":
        insights = build_peer_review_insights(db, exam)
        return templates.TemplateResponse(
            request,
            "results/peer_review.html",
            {
                "request": request,
                "active_nav": "insights",
                "exam": exam,
                "insights": insights,
            },
        )

    results = list(exam.results)
    results.sort(key=lambda r: (r.total_score is None, -(r.total_score or 0)))
    summary = summarize_results(results)
    analysis_rows = question_analysis(db, exam)
    analysis_sheets = _group_analysis_rows(analysis_rows)
    feedback_priority_rows = _feedback_priority_rows(analysis_rows)

    return templates.TemplateResponse(
        request,
        "results/index.html",
        {
            "request": request,
            "active_nav": "insights",
            "exam": exam,
            "results": results,
            "summary": summary,
            "analysis_sheets": analysis_sheets,
            "feedback_priority_rows": feedback_priority_rows,
        },
    )


def _group_analysis_rows(rows: list[dict]) -> list[dict]:
    sheets: list[dict] = []
    for row in rows:
        sheet = row["sheet"]
        if not sheets or sheets[-1]["sheet"] is not sheet:
            sheets.append({"sheet": sheet, "groups": []})
        groups = sheets[-1]["groups"]
        if not groups or groups[-1]["label"] != row["group_label"]:
            groups.append({"label": row["group_label"], "rows": []})
        groups[-1]["rows"].append(row)
    return sheets


def _feedback_priority_rows(rows: list[dict], limit: int = 15) -> list[dict]:
    priority_rows = [row for row in rows if row.get("priority_score") is not None]
    priority_rows.sort(key=lambda row: row["priority_score"], reverse=True)
    return priority_rows[:limit]


@router.post("/exams/{exam_id}/results/pass-mark")
def set_pass_mark(
    request: Request,
    exam_id: int,
    pass_mark_type: str | None = Form(None),
    pass_mark_value: float | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Set/clear the explicit pass mark for a Standard Test run and recalculate."""
    exam = get_run_or_404(db, exam_id, for_write=True)
    if exam.assessment_type == "peer_review":
        raise HTTPException(status_code=400)
    if pass_mark_type not in (None, "points", "percentage"):
        raise HTTPException(status_code=400)
    exam.pass_mark_type = pass_mark_type if pass_mark_value is not None else None
    exam.pass_mark_value = pass_mark_value if pass_mark_type else None
    recalculate_exam_results(db, exam)
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/results", status_code=303)


@router.post("/exams/{exam_id}/results/recalculate")
def recalculate_results(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    if exam.assessment_type == "peer_review":
        form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
        if form:
            recalculate_anonymous_peer_review_results(db, form)
            refresh_peer_review_exam_status(db, exam, form)
            return RedirectResponse(f"/exams/{exam_id}/results", status_code=303)
        recalculate_exam_results(db, exam)
    else:
        recalculate_exam_results(db, exam)
    exam.status = "results_ready"
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/results", status_code=303)
