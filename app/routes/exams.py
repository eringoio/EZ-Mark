import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.services.archive_service import archive_exam
from app.services.demo_service import ensure_uploads_allowed
from app.services.exam_service import (
    ExamCreateInput,
    ExamUpdateInput,
    create_exam,
    delete_exam,
    get_exam,
    get_run_or_404,
    list_exams,
    list_exams_paginated,
    next_exam_action,
    update_exam,
)
from app.services.i18n_service import _
from app.services.list_filter_service import (
    filter_context,
    month_fallback_filters,
    parse_list_filters,
)
from app.services.portable_package_service import (
    PackageError,
    export_session_package,
    import_session_package,
    read_package,
    session_preview,
)
from app.services.review_service import count_reviewable
from app.services.sheet_generation_service import (
    latest_exam_download_path,
    latest_sheet_generated_at,
)
from app.services.templates import templates

router = APIRouter()
settings = get_settings()

# Uploaded session packages are parked here between preview and confirm (relative to data/).
PACKAGE_PREVIEW_DIR = "import_previews"
PACKAGE_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


@router.get("/exams", response_class=HTMLResponse)
def exams_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    filters = parse_list_filters(request.query_params)
    paginated = list_exams_paginated(db, filters)
    exams = paginated.items
    month_fallback_sessions = (
        list_exams(db, month_fallback_filters(filters))
        if filters.date and not exams
        else []
    )
    return templates.TemplateResponse(
        request,
        "exams/index.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exams": exams,
            "month_fallback_sessions": month_fallback_sessions,
            "next_exam_action": next_exam_action,
            "filters": filter_context(filters),
            "filter_action": "/exams",
            "pagination": paginated,
        },
    )


@router.post("/exams/bulk/archive", response_model=None)
def bulk_archive_exams(
    selected_ids: list[int] = Form(default=[]),
    return_to: str = Form("/exams"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    for exam_id in selected_ids:
        exam = get_exam(db, exam_id)
        if exam is None or exam.is_template or exam.status == "archived":
            continue
        archive_exam(db, exam)
    return RedirectResponse(return_to or "/exams", status_code=303)


@router.post("/exams/bulk/delete", response_model=None)
def bulk_delete_exams(
    selected_ids: list[int] = Form(default=[]),
    return_to: str = Form("/exams"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    for exam_id in selected_ids:
        exam = get_exam(db, exam_id)
        if exam is None or exam.is_template:
            continue
        delete_exam(db, exam)
    return RedirectResponse(return_to or "/exams", status_code=303)


@router.get("/assessments/new", response_class=HTMLResponse)
@router.get("/exams/new", response_class=HTMLResponse)
def new_assessment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "exams/new.html",
        {"request": request, "active_nav": "sessions", "errors": []},
    )


@router.post("/assessments", response_model=None)
@router.post("/exams", response_model=None)
def create_assessment(
    request: Request,
    name: str = Form(...),
    assessment_type: str = Form("generic_test"),
    exam_date: str | None = Form(None),
    numbering_mode: str = Form("continuous"),
    db: Session = Depends(get_db),
) -> Response:
    scoring_mode = {"generic_test": "generic_raw", "peer_review": "peer_average"}.get(
        assessment_type, "generic_raw"
    )
    data = ExamCreateInput(
        name=name,
        assessment_type=assessment_type,
        exam_date=exam_date or None,
        scoring_mode=scoring_mode,
        numbering_mode=numbering_mode,
    )
    try:
        exam = create_exam(db, data)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "exams/new.html",
            {
                "request": request,
                "active_nav": "sessions",
                "errors": [str(exc)],
                "form": data,
            },
            status_code=400,
        )
    return RedirectResponse(f"/exams/{exam.id}", status_code=303)


@router.get("/exams/{exam_id}", response_class=HTMLResponse)
def exam_detail(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_exam(db, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)
    action_label, action_url = next_exam_action(exam)
    scanned_page_count = _scanned_page_count(db, exam)
    return templates.TemplateResponse(
        request,
        "exams/detail.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "action_label": action_label,
            "action_url": action_url,
            "download_path": latest_exam_download_path(db, exam),
            "last_generated_at": latest_sheet_generated_at(db, exam),
            "review_count": count_reviewable(db, exam.id),
            "scanned_page_count": scanned_page_count,
        },
    )


def _scanned_page_count(db: Session, exam) -> int:
    if exam.assessment_type != "peer_review":
        return sum(batch.total_pages for batch in exam.scan_batches)
    from app.models import PeerReviewForm
    from app.routes.dashboard import _peer_review_scanned_page_count

    form = db.scalar(select(PeerReviewForm).where(PeerReviewForm.exam_id == exam.id))
    if form is None:
        return 0
    return _peer_review_scanned_page_count(db, form.id)


@router.get("/exams/{exam_id}/edit", response_class=HTMLResponse)
def edit_exam_form(request: Request, exam_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = get_run_or_404(db, exam_id, for_write=True)
    return templates.TemplateResponse(
        request,
        "exams/edit.html",
        {"request": request, "active_nav": "sessions", "exam": exam, "errors": []},
    )


@router.post("/exams/{exam_id}/update", response_model=None)
def update_exam_route(
    request: Request,
    exam_id: int,
    name: str = Form(...),
    exam_date: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    exam = get_run_or_404(db, exam_id, for_write=True)
    data = ExamUpdateInput(name=name, exam_date=exam_date or None)
    try:
        update_exam(db, exam, data)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "exams/edit.html",
            {"request": request, "active_nav": "sessions", "exam": exam, "errors": [str(exc)]},
            status_code=400,
        )
    return RedirectResponse(f"/exams/{exam_id}", status_code=303)


@router.post("/exams/{exam_id}/delete", response_model=None)
def delete_exam_route(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    exam = get_run_or_404(db, exam_id)
    delete_exam(db, exam)
    return RedirectResponse("/exams", status_code=303)


@router.post("/exams/{exam_id}/archive", response_model=None)
def archive_exam_route(exam_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    # Archiving removes scan files from disk but keeps result/insight data. Idempotent:
    # an already-archived session is left unchanged. Redirect to the archived listing so
    # the now-hidden session does not 404 the teacher on the (default) sessions page.
    exam = get_run_or_404(db, exam_id)
    archive_exam(db, exam)
    return RedirectResponse("/exams?archived=only", status_code=303)


# --- Portable session packages (.ezmark-session.zip) --------------------------
#
# A session package bundles the session together with its template so both can be
# recreated on another EZ Mark install. Export is allowed for archived sessions (their
# scan files are already purged, so the package simply omits scans). Import always
# creates a new active template and a new active session linked to it via
# source_template_id, with every id freshly remapped.


@router.get("/exams/{exam_id}/portable-export", response_model=None)
def portable_export_session(
    exam_id: int, include_scan_files: bool = False, db: Session = Depends(get_db)
) -> FileResponse:
    exam = get_run_or_404(db, exam_id)  # archived allowed (export is read-only)
    path = export_session_package(db, exam, include_scan_files=include_scan_files)
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.get("/sessions/import-package", response_class=HTMLResponse)
def import_session_package_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "exams/package_import.html",
        {"request": request, "active_nav": "sessions", "errors": []},
    )


@router.post("/sessions/import-package/preview", response_class=HTMLResponse)
async def import_session_package_preview(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ensure_uploads_allowed()
    if not (file.filename or "").lower().endswith(".zip"):
        return templates.TemplateResponse(
            request,
            "exams/package_import.html",
            {"request": request, "active_nav": "sessions", "errors": [_("packages.invalid_package")]},
            status_code=400,
        )
    preview_dir = settings.data_root / PACKAGE_PREVIEW_DIR
    preview_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temp_path = preview_dir / f"{token}.zip"
    temp_path.write_bytes(await file.read())
    try:
        package = read_package(temp_path)
        if package["package_type"] != "session":
            raise PackageError(_("packages.wrong_type_session"))
        preview = session_preview(package)
    except PackageError as exc:
        temp_path.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request,
            "exams/package_import.html",
            {"request": request, "active_nav": "sessions", "errors": [str(exc)]},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "exams/package_preview.html",
        {"request": request, "active_nav": "sessions", "preview": preview, "token": token},
    )


@router.post("/sessions/import-package/confirm", response_model=None)
def import_session_package_confirm(
    request: Request, token: str = Form(...), db: Session = Depends(get_db)
) -> Response:
    ensure_uploads_allowed()
    if not PACKAGE_TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail=_("packages.invalid_token"))
    temp_path = settings.data_root / PACKAGE_PREVIEW_DIR / f"{token}.zip"
    if not temp_path.exists():
        raise HTTPException(status_code=404, detail=_("packages.expired"))
    try:
        package = read_package(temp_path)
        template, session = import_session_package(db, package)
        import_warnings = package.get("import_warnings", [])
    except PackageError as exc:
        return templates.TemplateResponse(
            request,
            "exams/package_import.html",
            {"request": request, "active_nav": "sessions", "errors": [str(exc)]},
            status_code=400,
        )
    finally:
        temp_path.unlink(missing_ok=True)
    return templates.TemplateResponse(
        request,
        "exams/package_imported.html",
        {
            "request": request,
            "active_nav": "sessions",
            "template": template,
            "session": session,
            "qr_collision_warning_count": len(import_warnings),
        },
    )


@router.get("/exams/{exam_id}/competencies", response_class=HTMLResponse)
def competencies_index(
    request: Request, exam_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    from sqlalchemy import select

    from app.models import Competency

    exam = get_run_or_404(db, exam_id)
    competencies = list(
        db.scalars(
            select(Competency)
            .where(Competency.exam_id == exam_id)
            .order_by(Competency.display_order)
        )
    )
    return templates.TemplateResponse(
        request,
        "exams/competencies.html",
        {
            "request": request,
            "active_nav": "sessions",
            "exam": exam,
            "competencies": competencies,
        },
    )


@router.post("/exams/{exam_id}/competencies")
def add_competency(
    exam_id: int,
    name: str = Form(...),
    description: str | None = Form(None),
    min_score: int = Form(1),
    max_score: int = Form(5),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from sqlalchemy import select

    from app.models import Competency

    exam = get_exam(db, exam_id)
    if exam is None:
        raise HTTPException(status_code=404)

    # Get max display order
    max_order = (
        db.scalar(
            select(Competency.display_order)
            .where(Competency.exam_id == exam_id)
            .order_by(Competency.display_order.desc())
            .limit(1)
        )
        or 0
    )

    comp = Competency(
        exam_id=exam_id,
        name=name.strip(),
        description=description.strip() if description else None,
        min_score=min_score,
        max_score=max_score,
        display_order=max_order + 1,
    )
    db.add(comp)
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}/competencies", status_code=303)


@router.post("/exams/{exam_id}/competencies/{comp_id}/delete")
def delete_competency(
    exam_id: int,
    comp_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from app.models import Competency

    get_run_or_404(db, exam_id, for_write=True)
    comp = db.get(Competency, comp_id)
    if comp and comp.exam_id == exam_id:
        db.delete(comp)
        db.commit()
    return RedirectResponse(f"/exams/{exam_id}/competencies", status_code=303)
