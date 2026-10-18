import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import data_relative_path, get_db
from app.models import (
    MockExam,
    PeerReviewBatch,
    PeerReviewFinalResult,
    PeerReviewGroupResult,
    PeerReviewRating,
    PeerReviewSubmissionPage,
)
from app.services.archive_service import is_purged_scan_path
from app.services.demo_service import ensure_uploads_allowed
from app.services.exam_service import ensure_not_archived
from app.services.export_service import export_path
from app.services.i18n_service import _
from app.services.peer_review_builder_service import (
    add_competency,
    add_page_break_before_target,
    add_rater_group,
    auto_paginate,
    builder_state,
    delete_competency,
    delete_rater_group,
    get_or_create_form,
    move_competency,
    peer_review_layout_payload,
    peer_review_locked_roster_pending,
    remove_page_breaks,
    update_competency,
    update_form_settings,
    update_rater_group,
    update_target,
)
from app.services.peer_review_export_service import (
    generate_peer_review_group_results_export,
    generate_peer_review_raw_ratings_export,
    generate_peer_review_summary_export,
)
from app.services.peer_review_review_service import (
    PROBLEM_STATUSES,
    apply_rating_correction,
    build_review_context,
    next_problem_rating_id,
    resolve_remaining_submission_ratings,
)
from app.services.peer_review_scan_service import (
    create_peer_review_batch,
    process_peer_review_batch,
)
from app.services.peer_review_scoring_service import recalculate_anonymous_peer_review_results
from app.services.peer_review_sheet_service import (
    LAYOUT_DOES_NOT_FIT_MESSAGE,
    NO_ACTIVE_TARGETS_MESSAGE,
    generate_peer_review_pdf,
    regenerate_peer_review_sheets,
)
from app.services.peer_review_workflow_service import refresh_peer_review_exam_status
from app.services.sheet_generation_service import get_generated_file, latest_exam_download_path
from app.services.templates import templates

router = APIRouter()


def _peer_exam(db: Session, exam_id: int, *, allow_archived: bool = False) -> MockExam:
    exam = db.get(MockExam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    if exam.assessment_type != "peer_review":
        raise HTTPException(status_code=400, detail="This route is only for peer review exams.")
    if not allow_archived:
        # Builder/scan/review/results-recalc are all operational actions; archived
        # peer-review sessions are read-only. Result exports below pass allow_archived=True.
        ensure_not_archived(exam)
    return exam


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _peer_review_progress(db: Session, form_id: int) -> dict:
    """Counts that drive the scan/review/results "what next" call-to-actions."""
    total_ratings = (
        db.scalar(
            select(func.count(PeerReviewRating.id)).where(PeerReviewRating.form_id == form_id)
        )
        or 0
    )
    needs_review = (
        db.scalar(
            select(func.count(PeerReviewRating.id))
            .where(PeerReviewRating.form_id == form_id)
            .where(PeerReviewRating.status.in_(PROBLEM_STATUSES))
        )
        or 0
    )
    has_results = bool(
        db.scalar(
            select(func.count(PeerReviewFinalResult.id)).where(
                PeerReviewFinalResult.form_id == form_id
            )
        )
        or db.scalar(
            select(func.count(PeerReviewGroupResult.id)).where(
                PeerReviewGroupResult.form_id == form_id
            )
        )
    )
    return {
        "total_ratings": total_ratings,
        "needs_review": needs_review,
        "ready_to_score": total_ratings > 0 and needs_review == 0,
        "has_results": has_results,
    }


def _render_builder(
    request: Request,
    db: Session,
    exam: MockExam,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    state = builder_state(db, exam)
    form = get_or_create_form(db, exam)
    return templates.TemplateResponse(
        request,
        "peer_review/builder.html",
        {
            "request": request,
            "exam": exam,
            "state": state,
            "layout": peer_review_layout_payload(db, exam),
            "download_path": latest_exam_download_path(db, exam),
            "error": error,
            # Non-fatal: the layout is locked but the roster has new students who are not on a
            # printed sheet yet. Shown as a warning so the teacher knows to regenerate.
            "roster_locked_pending": peer_review_locked_roster_pending(db, form),
        },
        status_code=status_code,
    )


@router.get("/exams/{exam_id}/peer-review", response_class=HTMLResponse)
def peer_review_home(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render_builder(request, db, _peer_exam(db, exam_id))


@router.get("/exams/{exam_id}/peer-review/layout")
def peer_review_session_layout(exam_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    return JSONResponse(peer_review_layout_payload(db, _peer_exam(db, exam_id)))


@router.get("/exams/{exam_id}/peer-review/builder", response_class=HTMLResponse)
def peer_review_builder(
    request: Request, exam_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    return peer_review_home(request, exam_id, db)


@router.get("/exams/{exam_id}/peer-review/builder/state")
def peer_review_builder_state(exam_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    return JSONResponse(builder_state(db, _peer_exam(db, exam_id)))


@router.post("/exams/{exam_id}/peer-review/builder/state")
async def save_peer_review_builder_state(
    request: Request, exam_id: int, db: Session = Depends(get_db)
) -> JSONResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    _mutate_peer_review_builder(update_form_settings, db, form, await request.json())
    return JSONResponse(builder_state(db, exam))


@router.post("/exams/{exam_id}/peer-review/settings")
def save_peer_review_settings(
    exam_id: int,
    title: str = Form(...),
    score_min: int = Form(...),
    score_max: int = Form(...),
    students_per_page: int = Form(...),
    page_orientation_mode: str = Form("auto"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    _mutate_peer_review_builder(
        update_form_settings,
        db,
        form,
        {
            "title": title.strip() or exam.name,
            "score_min": score_min,
            "score_max": score_max,
            "students_per_page": students_per_page,
            "page_orientation_mode": page_orientation_mode,
        },
    )
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/pages/auto-paginate")
def auto_paginate_peer_review(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(auto_paginate, db, form)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.get("/exams/{exam_id}/peer-review/scan-batches", response_class=HTMLResponse)
def peer_review_scan_batches(
    request: Request,
    exam_id: int,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    return templates.TemplateResponse(
        request,
        "peer_review/scan_batches.html",
        {
            "request": request,
            "exam": exam,
            "form": form,
            "download_path": latest_exam_download_path(db, exam),
            "progress": _peer_review_progress(db, form.id),
            "batches": db.query(PeerReviewBatch)
            .filter(PeerReviewBatch.form_id == form.id)
            .order_by(PeerReviewBatch.created_at.desc())
            .all(),
            "upload_errors": request.query_params.getlist("error"),
        },
    )


@router.post("/exams/{exam_id}/peer-review/scan-batches/upload")
async def upload_peer_review_scan_batch(
    exam_id: int,
    rater_group_id: int = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    ensure_uploads_allowed()
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    upload_dir = get_settings().data_root / "uploaded_scans" / f"peer_review_form_{form.id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for file in files:
        if not file.filename:
            continue
        content = await file.read()
        if not content:
            continue
        safe_name = Path(file.filename).name
        path = upload_dir / safe_name
        path.write_bytes(content)
        try:
            batch = create_peer_review_batch(
                db, form.id, rater_group_id, safe_name, data_relative_path(path)
            )
            process_peer_review_batch(db, batch)
        except (ValueError, Exception) as exc:  # noqa: BLE001 - surface per-file failures
            errors.append(f"{safe_name}: {exc}")
    refresh_peer_review_exam_status(db, exam, form)
    target = f"/exams/{exam_id}/peer-review/scan-batches"
    if errors:
        params = "&".join(f"error={e}" for e in errors[:3])
        return RedirectResponse(f"{target}?{params}", status_code=303)
    return RedirectResponse(target, status_code=303)


@router.get("/peer-review/submission-pages/{submission_id}/image")
def peer_review_submission_image(submission_id: int, db: Session = Depends(get_db)) -> FileResponse:
    submission = db.get(PeerReviewSubmissionPage, submission_id)
    if submission is None:
        raise HTTPException(status_code=404)
    relative = submission.aligned_image_path or submission.image_path
    if is_purged_scan_path(relative):
        # The session was archived and its scans were deleted from disk.
        raise HTTPException(status_code=410, detail="Scan files were removed when archived.")
    path = get_settings().data_root / relative
    if not path.exists():
        raise HTTPException(status_code=404, detail="Scan image not found.")
    return FileResponse(path, filename=path.name)


@router.get("/exams/{exam_id}/peer-review/review", response_class=HTMLResponse)
def peer_review_review(
    request: Request,
    exam_id: int,
    rating_id: int | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    context = build_review_context(db, form, rating_id)
    return templates.TemplateResponse(
        request,
        "peer_review/review.html",
        {
            "request": request,
            "exam": exam,
            "form": form,
            "progress": _peer_review_progress(db, form.id),
            **context,
        },
    )


@router.post("/exams/{exam_id}/peer-review/review/{rating_id}")
def save_peer_review_rating(
    exam_id: int,
    rating_id: int,
    score: int | None = Form(None),
    status: str = Form("reviewed"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    rating = db.get(PeerReviewRating, rating_id)
    if rating is None or rating.form_id != form.id:
        raise HTTPException(status_code=404)
    apply_rating_correction(
        db,
        rating,
        score=score,
        status=status,
        score_min=form.score_min,
        score_max=form.score_max,
    )
    refresh_peer_review_exam_status(db, exam, form)
    next_id = next_problem_rating_id(db, form, rating)
    target = f"/exams/{exam_id}/peer-review/review"
    if next_id is not None:
        target = f"{target}?rating_id={next_id}"
    return RedirectResponse(target, status_code=303)


@router.post("/exams/{exam_id}/peer-review/submission-pages/{submission_id}/mark-remaining-blank")
def mark_remaining_peer_review_page_blank(
    exam_id: int, submission_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    submission = db.get(PeerReviewSubmissionPage, submission_id)
    if (
        submission is None
        or submission.peer_review_page is None
        or submission.peer_review_page.form_id != form.id
    ):
        raise HTTPException(status_code=404)
    resolve_remaining_submission_ratings(db, submission_id, "blank")
    refresh_peer_review_exam_status(db, exam, form)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/review", status_code=303)


@router.post("/exams/{exam_id}/peer-review/submission-pages/{submission_id}/mark-remaining-invalid")
def mark_remaining_peer_review_page_invalid(
    exam_id: int, submission_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    submission = db.get(PeerReviewSubmissionPage, submission_id)
    if (
        submission is None
        or submission.peer_review_page is None
        or submission.peer_review_page.form_id != form.id
    ):
        raise HTTPException(status_code=404)
    resolve_remaining_submission_ratings(db, submission_id, "invalid")
    refresh_peer_review_exam_status(db, exam, form)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/review", status_code=303)


@router.post("/exams/{exam_id}/peer-review/pages/page-break")
def add_peer_review_page_break(
    exam_id: int,
    target_id: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(add_page_break_before_target, db, form, target_id)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/pages/page-break/remove")
def remove_peer_review_page_breaks(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(remove_page_breaks, db, form)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/groups")
def create_peer_review_group(
    exam_id: int,
    name: str = Form(...),
    weight_percent: float = Form(0),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    add_rater_group(db, form, name, weight_percent, description or None)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/groups/{group_id}")
def edit_peer_review_group(
    exam_id: int,
    group_id: int,
    name: str = Form(...),
    weight_percent: float = Form(0),
    description: str = Form(""),
    active: bool = Form(True),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(
        update_rater_group,
        db,
        form,
        group_id,
        {
            "name": name,
            "weight_percent": weight_percent,
            "description": description or None,
            "active": active,
        },
    )
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/groups/{group_id}/delete")
def delete_peer_review_group(
    exam_id: int,
    group_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    delete_rater_group(db, form, group_id)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/competencies")
def create_peer_review_competency(
    exam_id: int,
    name: str = Form(...),
    name_furigana: str = Form(""),
    short_label: str = Form(""),
    name_ruby_json: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(
        add_competency,
        db,
        form,
        name,
        description or None,
        name_furigana or None,
        short_label or None,
        _parse_ruby_json(name_ruby_json),
    )
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/competencies/{competency_id}")
def edit_peer_review_competency(
    exam_id: int,
    competency_id: int,
    name: str = Form(...),
    name_furigana: str = Form(""),
    short_label: str = Form(""),
    name_ruby_json: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(
        update_competency,
        db,
        form,
        competency_id,
        {
            "name": name,
            "name_furigana": name_furigana,
            "short_label": short_label,
            "name_ruby": _parse_ruby_json(name_ruby_json),
            "description": description or None,
        },
    )
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/competencies/{competency_id}/move")
def move_peer_review_competency(
    exam_id: int,
    competency_id: int,
    direction: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(move_competency, db, form, competency_id, direction)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/competencies/{competency_id}/delete")
def remove_peer_review_competency(
    exam_id: int,
    competency_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(delete_competency, db, form, competency_id)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/targets/{target_id}")
def edit_peer_review_target(
    exam_id: int,
    target_id: int,
    display_label: str = Form(...),
    active: bool = Form(True),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id))
    _mutate_peer_review_builder(
        update_target, db, form, target_id, {"display_label": display_label, "active": active}
    )
    _mutate_peer_review_builder(auto_paginate, db, form)
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/sheets/generate", response_model=None)
def generate_peer_review_sheets_route(
    request: Request, exam_id: int, db: Session = Depends(get_db)
) -> RedirectResponse | HTMLResponse | JSONResponse:
    exam = _peer_exam(db, exam_id)
    download_url = f"/exams/{exam_id}/peer-review/sheets/download/latest"
    try:
        generate_peer_review_pdf(db, exam)
    except ValueError as exc:
        message = _generate_error_message(str(exc))
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": message}, status_code=400)
        return _render_builder(request, db, exam, error=message, status_code=400)
    if _wants_json(request):
        return JSONResponse({"ok": True, "download": download_url})
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


@router.post("/exams/{exam_id}/peer-review/sheets/regenerate", response_model=None)
def regenerate_peer_review_sheets_route(
    request: Request, exam_id: int, db: Session = Depends(get_db)
) -> RedirectResponse | HTMLResponse | JSONResponse:
    exam = _peer_exam(db, exam_id)
    download_url = f"/exams/{exam_id}/peer-review/sheets/download/latest"
    try:
        regenerate_peer_review_sheets(db, exam)
    except ValueError as exc:
        message = _generate_error_message(str(exc))
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": message}, status_code=400)
        return _render_builder(request, db, exam, error=message, status_code=400)
    if _wants_json(request):
        return JSONResponse({"ok": True, "download": download_url})
    return RedirectResponse(f"/exams/{exam_id}/peer-review/builder", status_code=303)


def _generate_error_message(code: str) -> str:
    if code == NO_ACTIVE_TARGETS_MESSAGE:
        return _("peer_review.no_targets_error")
    if code == LAYOUT_DOES_NOT_FIT_MESSAGE:
        return _("peer_review.does_not_fit")
    return code


@router.get("/exams/{exam_id}/peer-review/sheets/download/latest")
def download_latest_peer_review_pdf(exam_id: int, db: Session = Depends(get_db)) -> FileResponse:
    exam = _peer_exam(db, exam_id)
    relative_path = latest_exam_download_path(db, exam)
    if not relative_path:
        raise HTTPException(status_code=404, detail="No peer-review PDF has been generated yet.")
    path = get_generated_file(relative_path)
    return FileResponse(path, filename=path.name)


@router.post("/exams/{exam_id}/peer-review/results/recalculate")
def recalculate_peer_review_results_route(
    exam_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    exam = _peer_exam(db, exam_id)
    form = get_or_create_form(db, exam)
    recalculate_anonymous_peer_review_results(db, form)
    # Surface peer-review results on the shared insights page just like a normal test.
    refresh_peer_review_exam_status(db, exam, form)
    return RedirectResponse(f"/exams/{exam_id}/results", status_code=303)


@router.get("/exams/{exam_id}/peer-review/exports/summary.xlsx")
def peer_review_summary_export(exam_id: int, db: Session = Depends(get_db)) -> FileResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id, allow_archived=True))
    path = export_path(generate_peer_review_summary_export(db, form, "xlsx"))
    return FileResponse(path, filename=path.name)


@router.get("/exams/{exam_id}/peer-review/exports/raw-ratings.xlsx")
def peer_review_raw_export(exam_id: int, db: Session = Depends(get_db)) -> FileResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id, allow_archived=True))
    path = export_path(generate_peer_review_raw_ratings_export(db, form, "xlsx"))
    return FileResponse(path, filename=path.name)


@router.get("/exams/{exam_id}/peer-review/exports/group-results.xlsx")
def peer_review_group_export(exam_id: int, db: Session = Depends(get_db)) -> FileResponse:
    form = get_or_create_form(db, _peer_exam(db, exam_id, allow_archived=True))
    path = export_path(generate_peer_review_group_results_export(db, form, "xlsx"))
    return FileResponse(path, filename=path.name)


def _mutate_peer_review_builder(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_ruby_json(raw: str) -> list:
    """Parse the per-segment ruby payload (a JSON array string) into a list.

    Returns ``[]`` for blank or malformed input; the service normalizes/validates entries.
    """
    if not raw or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []
